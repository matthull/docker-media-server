"""Tests for stack_watch.py. Run: python3 -m unittest discover -s monitoring"""
import contextlib
import inspect
import io
import itertools
import json
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
from datetime import datetime, timedelta, timezone
# By name, not as http.client: `http` is a parameter name throughout these tests.
from http.client import IncompleteRead
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stack_watch as sw  # noqa: E402

NOW = datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc)
SEC, MIN, HOUR, DAY = timedelta(seconds=1), timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def raises(exc):
    """A fake `run` or `http` that always raises exc."""
    def fake(*args, **kwargs):
        raise exc
    return fake


def movie(**overrides):
    base = {"id": 1, "title": "Film", "year": 2024, "monitored": True, "hasFile": False,
            "isAvailable": True, "minimumAvailability": "released",
            "added": iso(NOW - timedelta(days=10)), "releaseDate": iso(NOW - timedelta(days=100))}
    return base | overrides


def episode(**overrides):
    base = {"id": 11, "seriesId": 5, "seasonNumber": 1, "episodeNumber": 2, "monitored": True,
            "hasFile": False, "airDateUtc": iso(NOW - timedelta(days=10)),
            "series": {"title": "Show", "added": iso(NOW - timedelta(days=30))}}
    return base | overrides


class FakeHttp:
    """Routes by URL substring and records every call. A list response is served one item per call.
    ntfy publishes succeed, unless fail_posts_after of them already have.

    `armed` names sequences with a message still scheduled and `delivered` ones already sent, and
    the poll/cancel pair behaves as ntfy.sh was measured to behave:

    * a cancel answers 200 whether it was honoured or not, and leaves a `message_delete` entry
      either way; an honoured one also removes the message;
    * `ignore_cancels` drops that many cancels first — the same-second blind spot the real service
      has, and the thing that makes a 200 unsafe to believe;
    * **the plain poll returns delivered history, and `scheduled=1` ADDS what is still waiting.** It
      does not return the waiting ones alone. A fake that got this backwards is what let a `pending`
      check that matched every heartbeat this host had ever delivered pass its tests and then report
      a successful cancel as a failure on the first live run.

    Each armed message gets an id distinct from the delivered ones, because telling the two feeds
    apart by id is the only thing that separates "still waiting" from "went out last week"."""

    def __init__(self, routes=None, fail_posts_after=None, armed=(), delivered=(), ignore_cancels=0,
                 poll_error=None):
        self.routes = routes or {}
        self.fail_posts_after = fail_posts_after
        self.armed = list(armed)
        self.delivered = list(delivered)
        self.ignore_cancels = ignore_cancels
        self.poll_error = poll_error
        self.cancelled = []
        self.calls, self.sent = [], []

    def __call__(self, method, url, headers=None, data=None, timeout=20):
        self.calls.append((method, url, headers, data))
        if url.startswith("https://ntfy.test"):
            if method == "POST":
                if self.fail_posts_after is not None and len(self.sent) >= self.fail_posts_after:
                    raise urllib.error.URLError("ntfy down")
                self.sent.append(json.loads(data))
            elif method == "DELETE":
                sequence = url.rsplit("/", 1)[-1]
                self.cancelled.append(sequence)
                if self.ignore_cancels > 0:
                    self.ignore_cancels -= 1
                elif sequence in self.armed:
                    self.armed.remove(sequence)
            elif "poll=1" in url:
                if self.poll_error is not None:
                    raise self.poll_error
                events = ([{"id": f"del{i}", "sequence_id": s, "event": "message_delete"}
                           for i, s in enumerate(self.cancelled)]
                          + [{"id": f"old{i}", "sequence_id": s, "event": "message", "title": "x"}
                             for i, s in enumerate(self.delivered)])
                if "scheduled=1" in url:  # adds what is still waiting; never returns it alone
                    events += [{"id": f"new{i}", "sequence_id": s, "event": "message", "title": "x"}
                               for i, s in enumerate(self.armed)]
                return "\n".join(json.dumps(event) for event in events)
            return "{}"
        for fragment, response in self.routes.items():
            if fragment in url:
                if isinstance(response, list):
                    response = response.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response if isinstance(response, str) else json.dumps(response)
        raise AssertionError(f"unexpected request {url}")

    def published(self):
        return self.sent

    def deletes(self):
        return [url for method, url, _, _ in self.calls if method == "DELETE"]


def make_watch(cfg=None, http=None, run=None, now=NOW, timing=sw.REAL_TIMING):
    http = FakeHttp() if http is None else http
    return sw.Watch(cfg or {}, sw.Notifier("https://ntfy.test", "topic", http), now, http, run, "h", "h",
                    timing)


def paged(records):
    return {"page": 1, "pageSize": 250, "totalRecords": len(records), "records": records}


def container(service, status="running", health=None, exit_code=0, oneoff=False, id=None, by_hand=False):
    """`by_hand` is a `docker run` of an image Compose built: Compose stamps the project and service
    labels on the image, so the container has those, but not the ones Compose puts on containers."""
    state = {"Status": status, "ExitCode": exit_code}
    if health:
        state["Health"] = {"Status": health}
    labels = {"com.docker.compose.project": "proj", "com.docker.compose.service": service}
    if not by_hand:
        labels["com.docker.compose.oneoff"] = "True" if oneoff else "False"
    # Never the service name, so a check that reaches a container by name can't pass by accident.
    return {"Id": id or f"{service}-{status}-0123abcd", "Config": {"Labels": labels}, "State": state}


COMPOSE_CONFIG = json.dumps({"name": "proj", "services": {"sonarr": {}, "radarr": {}}})


def docker(*containers):
    """A fake `run` for a daemon that has these containers in the Compose project."""
    def run(argv):
        if argv[1] == "compose":
            return COMPOSE_CONFIG
        if argv[1] == "ps":
            return "\n".join(str(n) for n in range(len(containers)))
        return json.dumps(list(containers))
    return run


def docker_down(argv):
    if argv[1] == "compose":
        return COMPOSE_CONFIG  # parsing the compose file needs no daemon
    raise RuntimeError("Cannot connect to the Docker daemon at SENTINEL_SECRET")


class EnvFileTest(unittest.TestCase):
    def test_parses_quotes_comments_and_export(self):
        env = sw.parse_env_file(
            "# comment\nA=1\nexport B=two\nC='p$ss # not a comment'\nD=\"x\"\nE=val # note\n"
            "F=\"quoted\" # note\n\nnoise\n")
        self.assertEqual(env, {"A": "1", "B": "two", "C": "p$ss # not a comment", "D": "x",
                               "E": "val", "F": "quoted"})

    def test_environment_overrides_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".env").write_text("NTFY_TOPIC=file\nSTALL_DAYS=3\n")
            cfg = sw.load_config(Path(d), {"NTFY_TOPIC": "env"})
        self.assertEqual((cfg["NTFY_TOPIC"], cfg["STALL_DAYS"]), ("env", "3"))


class DurationTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual([sw.parse_duration(t) for t in ("45s", "90m", "24h", "2d")],
                         [45, 5400, 86400, 172800])

    def test_rejects_garbage(self):
        for text in ("24", "1w", "h", "1.5h"):
            with self.assertRaises(ValueError):
                sw.parse_duration(text)

    def test_span(self):
        self.assertEqual([sw.span(t) for t in (10 * SEC, 30 * MIN, 71 * HOUR, 3 * DAY, 90 * SEC)],
                         ["10s", "30m", "71h", "3d", "90s"])


class SizeTest(unittest.TestCase):
    def test_units_and_decimals(self):
        self.assertEqual(sw.parse_size("100G"), 100 * 1024 ** 3)
        self.assertEqual(sw.parse_size(" 2 T "), 2 * 1024 ** 4)
        self.assertEqual(sw.parse_size("512M"), 512 * 1024 ** 2)
        self.assertEqual(sw.parse_size("1.5G"), int(1.5 * 1024 ** 3))
        self.assertEqual(sw.parse_size("100g"), 100 * 1024 ** 3)

    def test_rejects_garbage(self):
        for text in ("", "100", "100K", "100GB", "lots", "-5G", "1e3G"):
            with self.assertRaises(ValueError):
                sw.parse_size(text)

    def test_size_renders_in_parse_sizes_format(self):
        self.assertEqual(sw.size(100 * 1024 ** 3), "100G")
        self.assertEqual(sw.size(25 * 1024 ** 3), "25G")
        self.assertEqual(sw.size(2 * 1024 ** 4), "2T")
        self.assertEqual(sw.size(512 * 1024 ** 2), "512M")
        self.assertEqual(sw.size(int(1.5 * 1024 ** 3)), "1.5G")
        for count in (1, 25, 100, 512):
            for unit in "MGT":
                self.assertEqual(sw.size(sw.parse_size(f"{count}{unit}")), f"{count}{unit}")

    def test_a_measured_figure_is_rounded_to_something_readable_on_a_phone(self):
        """Every other size here is derived from DISK_FREE_MIN and lands on a round number. Free
        space is read off the filesystem and does not: a live check on this host rendered
        "142.5914421G free", which the whole test suite missed because every drive in it is a round
        number of gigabytes. Four significant digits, because a value needing five in one unit has
        already reached the next one, so this is also the widest that never turns into an exponent."""
        self.assertEqual(sw.size(153106874368, sw.MEASURED), "142.6G")
        self.assertEqual(sw.size(16374452224, sw.MEASURED), "15.25G")
        self.assertEqual(sw.size(5 * 1024 ** 3, sw.MEASURED), "5G")
        # The widest value each unit can hold, which is where %g reaches for an exponent.
        self.assertEqual(sw.size(1024 ** 4 - 1, sw.MEASURED), "1024G")
        self.assertEqual(sw.size(1024 ** 3 - 1, sw.MEASURED), "1024M")


def unit_start_timeout():
    """TimeoutStartSec of the units install-stack-watch.sh writes, in seconds."""
    installer = Path(sw.__file__).with_name("install-stack-watch.sh").read_text()
    [limit] = re.findall(r"^TimeoutStartSec=(\d+)min$", installer, re.MULTILINE)
    return int(limit) * 60


class ScheduleTest(unittest.TestCase):
    def test_check_timer_runs_every_check_interval(self):
        installer = Path(sw.__file__).with_name("install-stack-watch.sh").read_text()
        minutes = re.findall(r"^OnCalendar=\*:0/(\d+)$", installer, re.MULTILINE)
        self.assertEqual([timedelta(minutes=int(m)) for m in minutes], [sw.CHECK_INTERVAL])

    def test_a_run_is_given_up_on_before_the_next_one_is_due(self):
        """systemd does not start a oneshot that is still running, so a run allowed to outlast the
        interval would silently swallow the next tick instead of failing."""
        self.assertLess(unit_start_timeout(), sw.CHECK_INTERVAL.total_seconds())


class StalledMoviesTest(unittest.TestCase):
    def keys(self, movies, queued=(), days=3):
        return [s.key for s in sw.stalled_movies(movies, set(queued), NOW, days)]

    def test_stalls_only_past_the_limit(self):
        at_limit = movie(id=1, added=iso(NOW - timedelta(days=3)))
        past_limit = movie(id=2, added=iso(NOW - timedelta(days=3, seconds=1)))
        self.assertEqual(self.keys([at_limit, past_limit]), ["radarr:2"])

    def test_skips_files_unmonitored_unavailable_and_queued(self):
        movies = [movie(id=1, hasFile=True), movie(id=2, monitored=False),
                  movie(id=3, isAvailable=False), movie(id=4), movie(id=5)]
        self.assertEqual(self.keys(movies, queued=[4]), ["radarr:5"])

    def test_clock_starts_at_release_when_added_before_release(self):
        recent_release = movie(added=iso(NOW - timedelta(days=60)),
                               releaseDate=iso(NOW - timedelta(days=1)))
        self.assertEqual(self.keys([recent_release]), [])
        stalled = sw.stalled_movies([movie()], set(), NOW, 3)[0]
        self.assertEqual((stalled.group, stalled.since), ("Film (2024)", NOW - timedelta(days=10)))

    def test_available_since_follows_minimum_availability(self):
        dates = {"inCinemas": iso(NOW - timedelta(days=50)),
                 "digitalRelease": iso(NOW - timedelta(days=20)),
                 "physicalRelease": iso(NOW - timedelta(days=30))}
        self.assertEqual(sw.available_since(dict(dates, minimumAvailability="inCinemas")),
                         NOW - timedelta(days=50))
        self.assertEqual(sw.available_since(dict(dates, minimumAvailability="released")),
                         NOW - timedelta(days=30))
        self.assertEqual(sw.available_since(dict(dates, minimumAvailability="released",
                                                 releaseDate=iso(NOW - timedelta(days=5)))),
                         NOW - timedelta(days=5))
        self.assertIsNone(sw.available_since(dict(dates, minimumAvailability="announced")))
        self.assertIsNone(sw.available_since({"minimumAvailability": "released"}))

    def test_movie_with_no_dates_is_skipped_not_fatal(self):
        undated = movie(id=7, added=None, releaseDate=None)
        self.assertEqual(self.keys([undated, movie(id=8)]), ["radarr:8"])


class StalledEpisodesTest(unittest.TestCase):
    def test_clock_starts_at_later_of_air_date_and_series_added(self):
        old_air_new_series = episode(id=1, series={"title": "S", "added": iso(NOW - timedelta(days=1))})
        stalled = episode(id=2)
        unaired = episode(id=3, airDateUtc=None)
        queued = episode(id=4)
        with_file = episode(id=5, hasFile=True)
        unmonitored = episode(id=6, monitored=False)
        just_aired = episode(id=7, airDateUtc=iso(NOW - timedelta(days=2)))
        result = sw.stalled_episodes(
            [old_air_new_series, stalled, unaired, queued, with_file, unmonitored, just_aired],
            {4}, NOW, 3)
        self.assertEqual([(s.key, s.group, s.detail) for s in result], [("sonarr:2", "Show", "S01E02")])


class DigestTest(unittest.TestCase):
    item = sw.Stalled("radarr:1", "Film (2024)", "", NOW - timedelta(days=10))

    @staticmethod
    def notified(ago, sent=1):
        return {"radarr:1": {"at": (NOW - ago).isoformat(), "sent": sent}}

    def test_due_for_new_items_and_reminders(self):
        self.assertTrue(sw.digest_due([self.item], {}, NOW, 7, 2))
        recent = self.notified(timedelta(days=7) - SEC)
        self.assertFalse(sw.digest_due([self.item], recent, NOW, 7, 2))
        self.assertTrue(sw.digest_due([self.item], self.notified(timedelta(days=7)), NOW, 7, 2))

    def test_reminders_stop_after_the_cap(self):
        self.assertTrue(sw.digest_due([self.item], self.notified(timedelta(days=7), sent=2), NOW, 7, 2))
        self.assertFalse(sw.digest_due([self.item], self.notified(timedelta(days=70), sent=3), NOW, 7, 2))

    def test_long_digest_fits_as_many_lines_as_it_can_new_ones_first(self):
        items = [sw.Stalled(f"radarr:{n}", f"A film with quite a long title, number {n:03d}", "",
                            NOW - timedelta(days=5)) for n in range(200)]
        notified = {i.key: {"at": NOW.isoformat(), "sent": 1} for i in items[:-1]}
        title, message = sw.format_digest(items, notified, 3)
        self.assertEqual(title, "200 wanted titles not downloaded after 3 days")
        self.assertEqual(sw.MAX_MESSAGE_BYTES, 4096)
        size = len(message.encode())
        self.assertLessEqual(size, sw.MAX_MESSAGE_BYTES)
        body, footer = message.split("\n\n", 1)
        self.assertEqual(footer, "Usually no release exists at the allowed quality yet, or the indexer is "
                                 "not returning results. Details: Sonarr/Radarr > Wanted > Missing. "
                                 "Unmonitor a title there to stop hearing about it.")
        *shown, more = body.split("\n")
        hidden = int(re.fullmatch(r"… and (\d+) more", more)[1])
        self.assertEqual(len(shown) + hidden, 200)
        self.assertTrue(shown[0].startswith("• A film with quite a long title, number 199, waiting"))
        self.assertTrue(shown[0].endswith("(new)"))
        self.assertTrue(shown[1].startswith("• A film with quite a long title, number 000, waiting"))
        self.assertGreater(size + len(shown[1].encode()) + 1, sw.MAX_MESSAGE_BYTES)  # nothing more fits

    def test_format_groups_episodes_and_puts_new_titles_first(self):
        since = NOW - timedelta(days=4)
        items = [sw.Stalled(f"sonarr:{n}", "Big Show", f"S01E0{n}", since) for n in range(1, 6)]
        items += [sw.Stalled("sonarr:20", "Small Show", "S02E01", since),
                  sw.Stalled("sonarr:21", "Small Show", "S02E02", since), self.item]
        entry = {"at": NOW.isoformat(), "sent": 1}
        notified = {f"sonarr:{n}": entry for n in range(1, 6)} | {"sonarr:20": entry, "radarr:1": entry}
        title, message = sw.format_digest(items, notified, 3)
        self.assertEqual(title, "3 wanted titles not downloaded after 3 days")
        waiting = f", waiting since {sw.when(since)}"
        self.assertEqual(message.split("\n\n")[0].splitlines(), [
            f"• Small Show: S02E01, S02E02{waiting} (new)",
            f"• Big Show: 5 episodes{waiting}",
            f"• Film (2024), waiting since {sw.when(NOW - timedelta(days=10))}",
        ])
        self.assertEqual(sw.format_digest([self.item], {}, 1)[0],
                         "1 wanted title not downloaded after 1 days")


class ArrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"CONFIG_ROOT": self.tmp.name, "STACK_DIR": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, name, xml, root=None):
        conf = Path(root or self.tmp.name, "config", name)
        conf.mkdir(parents=True)
        conf.joinpath("config.xml").write_text(xml)

    def test_reads_port_urlbase_key_and_pages(self):
        self.write_config("radarr",
                          "<Config><Port>7878</Port><UrlBase>/radarr</UrlBase><ApiKey>k123</ApiKey></Config>")
        http = FakeHttp({"wanted/missing": [
            {"totalRecords": 3, "pageSize": 2, "records": [{"id": 1}, {"id": 2}]},
            {"totalRecords": 3, "pageSize": 2, "records": [{"id": 3}]}]})
        arr = sw.Arr.from_config(self.cfg, "radarr", http)
        self.assertEqual([r["id"] for r in arr.all_records("wanted/missing", monitored=True)], [1, 2, 3])
        first_url, headers = http.calls[0][1], http.calls[0][2]
        self.assertTrue(first_url.startswith("http://localhost:7878/radarr/api/v3/wanted/missing?"))
        self.assertIn("monitored=true", first_url)
        self.assertIn("page=2", http.calls[1][1])
        self.assertEqual(headers, {"X-Api-Key": "k123"})

    def test_url_override(self):
        self.write_config("sonarr", "<Port>8989</Port><ApiKey>k</ApiKey>")
        arr = sw.Arr.from_config(self.cfg | {"SONARR_URL": "http://nas:1/s/"}, "sonarr", None)
        self.assertEqual(arr.base, "http://nas:1/s")

    def test_a_reply_cut_short_by_a_restart_is_the_arrs_problem_not_the_watchs(self):
        """Same class as the Jellyfin case: HTTPException is not an OSError, so it escaped the
        handler and the digest reported the watch as failing rather than the arr as unreachable."""
        self.write_config("radarr", "<Port>7878</Port><ApiKey>k</ApiKey>")
        for failure in (sw.HTTPException("BadStatusLine"), IncompleteRead(b"partial")):
            with self.subTest(failure=type(failure).__name__):
                arr = sw.Arr.from_config(self.cfg, "radarr", FakeHttp({"queue": failure}))
                with self.assertRaisesRegex(sw.WatchError, "^radarr: no usable answer from its API$"):
                    arr.get("queue")

    def test_missing_config_is_an_error_not_silence(self):
        with self.assertRaisesRegex(sw.WatchError, r"^sonarr: can't read its config.xml under CONFIG_ROOT$"):
            sw.Arr.from_config(self.cfg, "sonarr", None)

    def test_relative_config_root_is_under_the_stack_directory(self):
        self.write_config("sonarr", "<Port>8989</Port><ApiKey>relative</ApiKey>", Path(self.tmp.name, "cfg"))
        arr = sw.Arr.from_config({"CONFIG_ROOT": "cfg", "STACK_DIR": self.tmp.name}, "sonarr", None)
        self.assertEqual(arr.key, "relative")

    def test_api_errors_carry_labels_the_script_wrote(self):
        cases = [(urllib.error.HTTPError("http://r?SENTINEL_SECRET", 401, "SENTINEL_SECRET", {}, None),
                  "radarr: HTTP 401"),
                 (urllib.error.URLError("SENTINEL_SECRET"), "radarr: no usable answer from its API"),
                 ("<html>SENTINEL_SECRET", "radarr: no usable answer from its API")]
        for response, label in cases:
            with self.subTest(label=label):
                arr = sw.Arr("radarr", "http://r", "k", FakeHttp({"/api/v3/": response}))
                with self.assertRaises(sw.WatchError) as caught:
                    arr.get("queue")
                self.assertEqual(str(caught.exception), label)


class RunStalledTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name, port in (("radarr", 7878), ("sonarr", 8989)):
            conf = Path(self.tmp.name, "config", name)
            conf.mkdir(parents=True)
            conf.joinpath("config.xml").write_text(f"<Port>{port}</Port><ApiKey>k</ApiKey>")
        self.cfg = {"CONFIG_ROOT": self.tmp.name, "STACK_DIR": self.tmp.name, "STALL_DAYS": "3"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, state, movies, now=NOW, queue=()):
        http = FakeHttp({"7878/api/v3/queue": paged([{"movieId": q} for q in queue]),
                         "7878/api/v3/wanted/missing": paged(movies),
                         "8989/api/v3/queue": paged([]),
                         "8989/api/v3/wanted/missing": paged([episode()])})
        sw.run_stalled(make_watch(self.cfg, http, now=now), state)
        return http.published()

    def test_alerts_once_then_reminds_and_prunes(self):
        state = {}
        first = self.run_once(state, [movie()])
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["title"], "2 wanted titles not downloaded after 3 days")
        self.assertEqual(first[0]["priority"], 3)
        self.assertIn("Film (2024)", first[0]["message"])
        self.assertIn("Show: S01E02", first[0]["message"])
        self.assertEqual(set(state["notified"]), {"radarr:1", "sonarr:11"})

        self.assertEqual(self.run_once(state, [movie()], now=NOW + DAY), [])
        self.assertEqual(self.run_once(state, [movie()], now=NOW + timedelta(days=7) - SEC), [])
        self.assertEqual(len(self.run_once(state, [movie()], now=NOW + timedelta(days=7))), 1)

        self.assertEqual(self.run_once(state, [], now=NOW + timedelta(days=8)), [])
        self.assertEqual(set(state["notified"]), {"sonarr:11"})

    def test_new_item_resends_full_list(self):
        state = {}
        self.run_once(state, [movie()])
        again = self.run_once(state, [movie(), movie(id=2, title="Other")], now=NOW + HOUR)
        self.assertEqual(len(again), 1)
        self.assertIn("Other (2024), waiting since", again[0]["message"])
        self.assertIn("(new)", again[0]["message"])

    def test_queued_movie_is_not_stalled(self):
        sent = self.run_once({}, [movie()], queue=[1])
        self.assertNotIn("Film", sent[0]["message"])

    def test_two_reminders_then_quiet_until_a_new_title_arrives(self):
        state = {}
        counts = [len(self.run_once(state, [movie()], now=NOW + timedelta(days=d)))
                  for d in (0, 7, 14, 21, 28)]
        self.assertEqual(counts, [1, 1, 1, 0, 0])
        again = self.run_once(state, [movie(), movie(id=2, title="Other")], now=NOW + timedelta(days=29))
        self.assertEqual(len(again), 1)
        self.assertIn("Film (2024)", again[0]["message"])

    def test_upgrades_notified_entries_stored_as_strings(self):
        """d5c2dd1 stored the time alone; it must not crash the daily run."""
        state = {"notified": {"radarr:1": NOW.isoformat(), "sonarr:11": NOW.isoformat()}}
        self.assertEqual(self.run_once(state, [movie()], now=NOW + DAY), [])
        self.assertEqual(state["notified"]["radarr:1"], {"at": NOW.isoformat(), "sent": 1})


class ContainerProblemsTest(unittest.TestCase):
    def test_classifies_each_state(self):
        services = ["bazarr", "missing", "radarr", "recyclarr", "seerr", "sonarr", "sabnzbd", "prowlarr"]
        containers = [container("bazarr", "exited", exit_code=137), container("radarr", health="unhealthy"),
                      container("recyclarr"), container("seerr", health="starting"),
                      container("sonarr", health="healthy"), container("sabnzbd", "restarting"),
                      container("prowlarr", oneoff=True), container("not-expected", "exited")]
        self.assertEqual(sw.container_problems(services, containers), {
            "service:bazarr": "bazarr: exited (code 137)",
            "service:missing": "missing: no container",
            "service:radarr": "radarr: unhealthy",
            "service:sabnzbd": "sabnzbd: restarting",
            "service:prowlarr": "prowlarr: no container",
        })

    def test_a_container_run_by_hand_from_the_built_image_is_not_the_service(self):
        """`docker run docker-media-server-seerr` carries the image's Compose labels, so the project
        filter lists it. It is not what Compose runs, and must not stand in for a missing service."""
        self.assertEqual(sw.container_problems(["seerr"], [container("seerr", by_hand=True)]),
                         {"service:seerr": "seerr: no container"})

    def test_one_good_container_is_enough(self):
        containers = [container("sonarr", "exited"), container("sonarr", health="healthy")]
        self.assertEqual(sw.container_problems(["sonarr"], containers), {})


class HttpRequestTest(unittest.TestCase):
    """urlopen's timeout bounds each socket operation, not the request: measured, a reply trickling in
    a byte every 0.2s took 3.02s to arrive under timeout=0.5, and resolving the name has no timeout
    at all. RunTimeBudgetTest adds up the timeouts every call is given, which is only a bound on the
    run if each one really is a ceiling."""

    def trickle(self, seconds_per_byte, size):
        """A local server that answers with `size` bytes, one every `seconds_per_byte`. Returns its
        URL; the server gives up quietly if nobody is still reading."""
        server = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(server.close)

        def serve():
            conn, _ = server.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(f"HTTP/1.0 200 OK\r\nContent-Length: {size}\r\n\r\n".encode())
                with contextlib.suppress(OSError):
                    for _ in range(size):
                        time.sleep(seconds_per_byte)
                        conn.sendall(b"x")
        threading.Thread(target=serve, daemon=True).start()
        return f"http://127.0.0.1:{server.getsockname()[1]}/"

    def test_a_reply_that_keeps_trickling_in_is_given_up_on_at_the_timeout(self):
        url = self.trickle(0.1, 30)  # every read well inside the timeout; the whole reply 3s
        began = time.monotonic()
        with self.assertRaises(TimeoutError):
            sw.http_request("GET", url, timeout=0.5)
        self.assertLess(time.monotonic() - began, 2)

    def test_a_request_that_never_reaches_a_socket_is_given_up_on_too(self):
        """Name resolution happens before any socket timeout applies."""
        release = threading.Event()
        self.addCleanup(release.set)
        began = time.monotonic()
        with unittest.mock.patch.object(sw.urllib.request, "urlopen", lambda *a, **k: release.wait(10)):
            with self.assertRaises(TimeoutError):
                sw.http_request("GET", "http://resolving.invalid/", timeout=0.2)
        self.assertLess(time.monotonic() - began, 5)

    def test_the_abandoned_request_does_not_hold_up_the_interpreters_exit(self):
        """Otherwise the process outlives its own give-up by however long the request hangs, which
        is the overrun the timeout exists to prevent."""
        release = threading.Event()
        self.addCleanup(release.set)
        before = set(threading.enumerate())
        with unittest.mock.patch.object(sw.urllib.request, "urlopen", lambda *a, **k: release.wait(10)):
            with self.assertRaises(TimeoutError):
                sw.http_request("GET", "http://resolving.invalid/", timeout=0.2)
        [abandoned] = [t for t in threading.enumerate() if t not in before]
        self.assertTrue(abandoned.daemon)

    def test_an_answer_and_an_error_arrive_as_themselves(self):
        url = self.trickle(0, 3)
        self.assertEqual(sw.http_request("GET", url, timeout=5), "xxx")
        refused = urllib.error.HTTPError("http://x", 401, "no", {}, None)
        with unittest.mock.patch.object(sw.urllib.request, "urlopen", raises(refused)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                sw.http_request("GET", "http://x/")
        self.assertEqual(caught.exception.code, 401)  # Arr.get reads it

    def test_the_give_up_does_not_quote_the_url(self):
        """The ntfy URL carries the topic, and anyone who knows the topic can read and cancel on it."""
        release = threading.Event()
        self.addCleanup(release.set)
        with unittest.mock.patch.object(sw.urllib.request, "urlopen", lambda *a, **k: release.wait(10)):
            with self.assertRaises(TimeoutError) as caught:
                sw.http_request("POST", "https://ntfy.test/SENTINEL_TOPIC", timeout=0.2)
        self.assertNotIn("SENTINEL", str(caught.exception))


class JellyfinTest(unittest.TestCase):
    def test_states(self):
        self.assertIsNone(sw.jellyfin_problem("http://jf", FakeHttp({"/health": "Healthy\n"})))
        self.assertEqual(sw.jellyfin_problem("http://jf", FakeHttp({"/health": "Degraded"})),
                         "jellyfin: reports Degraded")
        self.assertEqual(sw.jellyfin_problem("http://jf", FakeHttp({"/health": "<html>SENTINEL_SECRET"})),
                         "jellyfin: unexpected answer from /health")
        refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sw.jellyfin_problem("http://jf", FakeHttp({"/health": refused})),
                             "jellyfin: not answering (details in the journal)")
        http_error = urllib.error.HTTPError("http://jf/health", 503, "x", {}, None)
        self.assertEqual(sw.jellyfin_problem("http://jf", FakeHttp({"/health": http_error})),
                         "jellyfin: health check returned HTTP 503")

    def test_a_reply_cut_short_by_a_restart_is_jellyfin_s_problem_not_the_watch_s(self):
        """http.client's exceptions are not OSError, and a restarting Jellyfin is exactly what
        raises one: the connection drops mid-reply. Uncaught it left jellyfin_problem, ended the
        run, took the container and disk checks down with it, and after CONSECUTIVE_RUNS announced
        "Stack watch is failing: IncompleteRead" — the watch blaming itself for the ordinary event
        it exists to report."""
        for failure in (sw.HTTPException("BadStatusLine"), IncompleteRead(b"partial")):
            with self.subTest(failure=type(failure).__name__), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(sw.jellyfin_problem("http://jf", FakeHttp({"/health": failure})),
                                 "jellyfin: not answering (details in the journal)")

    def test_an_unanswered_check_never_carries_the_os_error_text(self):
        """The URL is the one setting here that can name the host, and a failure to reach it is the
        one error whose text quotes it back: DNS puts the hostname in the message, TLS puts it in the
        certificate complaint. The topic is public, so neither can go out."""
        unreachable = [
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known: SENTINEL_HOST")),
            urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] hostname 'SENTINEL_HOST'"),
            urllib.error.URLError(OSError(13, "SENTINEL_SECRET")),
            TimeoutError(110, "timed out reaching SENTINEL_HOST"),
        ]
        for exc in unreachable:
            with self.subTest(exc=exc):
                with contextlib.redirect_stderr(io.StringIO()) as log:
                    problem = sw.jellyfin_problem("http://SENTINEL_HOST:8096",
                                                  FakeHttp({"/health": exc}))
                self.assertEqual(problem, "jellyfin: not answering (details in the journal)")
                self.assertNotIn("SENTINEL", problem)
                self.assertIn("SENTINEL", log.getvalue())  # it went to the journal instead


# Lines as they appear in Seerr v3.4.1's /app/dist/lib/downloadtracker.js, before and after
# images/seerr/patch-downloadtracker.sh deletes the write.
PATCHED_TRACKER = """\
                try {
                    const queueItems = await radarr.getQueue();
                    this.radarrServers[server.id] = queueItems.map((item) => ({
"""
UNPATCHED_TRACKER = PATCHED_TRACKER.replace(
    "                    const queueItems",
    "                    await radarr.refreshMonitoredDownloads();\n                    const queueItems")
SEERR_COMPOSE_CONFIG = json.dumps({"name": "proj", "services": {"sonarr": {}, "seerr": {}}})


def seerr_stack(*containers, tracker=PATCHED_TRACKER, config=SEERR_COMPOSE_CONFIG):
    """A fake `run` for a Compose project that includes Seerr. `tracker` is what reading the download
    tracker inside the container prints, or an exception that reading it raises. Records every call,
    with its keyword arguments, on .calls."""
    def run(argv, **kwargs):
        run.calls.append((argv, kwargs))
        if argv[1] == "compose":
            return config
        if argv[1] == "ps":
            return "\n".join(str(n) for n in range(len(containers)))
        if argv[1] == "exec":
            if isinstance(tracker, BaseException):
                raise tracker
            return tracker
        return json.dumps(list(containers))
    run.calls = []
    return run


def execs(run):
    return [argv for argv, _ in run.calls if argv[1] == "exec"]


class SeerrPatchTest(unittest.TestCase):
    """Seerr is built with its download tracker's arr write deleted (images/seerr/). Its healthcheck
    passes identically on an unpatched image, so without this a stale-but-healthy Seerr — the
    progress bar freezing while the requester watches — reports nothing anywhere."""

    def test_reads_the_file_the_build_patches_with_a_short_timeout(self):
        run = seerr_stack(tracker=PATCHED_TRACKER)
        self.assertIsNone(sw.seerr_patch_problem(run, "c0ffee"))
        self.assertEqual(run.calls, [(["docker", "exec", "c0ffee", "cat", "/app/dist/lib/downloadtracker.js"],
                                      {"timeout": sw.SEERR_EXEC_TIMEOUT})])

    def test_reads_the_container_the_health_check_found_not_one_named_seerr(self):
        """The health check finds the service's containers by Compose label, whatever they are
        called. Reading by container_name would read a different container, or none, as soon as
        that name changed or an unrelated container took it."""
        run = seerr_stack(container("sonarr"), container("seerr", id="4f1e"))
        self.assertEqual(sw.stack_problems(Path("/stack"), run), {})
        self.assertEqual(execs(run), [["docker", "exec", "4f1e", "cat", sw.SEERR_TRACKER]])

    def test_reads_the_running_container_when_the_service_has_several(self):
        for other in (container("seerr", "exited", id="old"), container("seerr", health="unhealthy", id="sick"),
                      container("seerr", oneoff=True, id="oneoff"), container("seerr", by_hand=True, id="negctl")):
            with self.subTest(other=other["Id"]):
                run = seerr_stack(container("sonarr"), other, container("seerr", id="live"), other)
                self.assertEqual(sw.stack_problems(Path("/stack"), run), {})
                self.assertEqual(execs(run), [["docker", "exec", "live", "cat", sw.SEERR_TRACKER]])

    def test_an_unpatched_tracker_is_a_problem(self):
        self.assertEqual(sw.seerr_patch_problem(seerr_stack(tracker=UNPATCHED_TRACKER), "c0ffee"),
                         "seerr: running without its download-tracker patch (see images/seerr/README.md)")

    def test_a_file_that_is_not_the_tracker_does_not_pass_for_a_patched_one(self):
        """The patch is an absence, and an absence is what an empty or unrelated file has too."""
        for source in ("", "module.exports = {};\n"):
            with self.subTest(source=source), contextlib.redirect_stderr(io.StringIO()) as log:
                self.assertEqual(sw.seerr_patch_problem(seerr_stack(tracker=source), "c0ffee"),
                                 "seerr: can't recognise its download tracker, so the patch is unchecked")
                self.assertIn("getQueue", log.getvalue())

    def test_a_failed_read_is_a_problem_that_carries_no_command_output(self):
        """`grep -c` would have been the obvious command, and it exits 1 exactly when the count is the
        0 that means patched — so a non-zero exit here has to be a real failure, never the answer."""
        failures = [RuntimeError("Error response from daemon: SENTINEL_SECRET"),
                    subprocess.TimeoutExpired(["docker", "exec", "SENTINEL_SECRET"], 5),
                    OSError(2, "SENTINEL_SECRET")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with contextlib.redirect_stderr(io.StringIO()) as log:
                    problem = sw.seerr_patch_problem(seerr_stack(tracker=failure), "c0ffee")
                self.assertEqual(problem, "seerr: can't check its download-tracker patch (details in the journal)")
                self.assertIn("SENTINEL", log.getvalue())  # the journal gets the reason

    def test_stack_problems_checks_a_running_seerr(self):
        run = seerr_stack(container("sonarr"), container("seerr", health="healthy"), tracker=UNPATCHED_TRACKER)
        self.assertEqual(sw.stack_problems(Path("/stack"), run), {
            "seerr:patch": "seerr: running without its download-tracker patch (see images/seerr/README.md)"})
        self.assertEqual(len(execs(run)), 1)

    def test_a_patched_seerr_adds_nothing(self):
        run = seerr_stack(container("sonarr"), container("seerr"))
        self.assertEqual(sw.stack_problems(Path("/stack"), run), {})
        self.assertEqual(len(execs(run)), 1)

    def test_a_seerr_that_is_not_running_is_reported_once_not_twice(self):
        """A stopped container can't be read, and it is already the problem being reported."""
        for state in (container("seerr", "exited", exit_code=1), container("seerr", health="unhealthy"),
                      container("seerr", "restarting")):
            with self.subTest(state=state["State"]):
                run = seerr_stack(container("sonarr"), state, tracker=RuntimeError("is not running"))
                self.assertEqual(list(sw.stack_problems(Path("/stack"), run)), ["service:seerr"])
                self.assertEqual(execs(run), [])

    def test_another_service_being_down_does_not_skip_the_check(self):
        run = seerr_stack(container("sonarr", "exited", exit_code=1), container("seerr"), tracker=UNPATCHED_TRACKER)
        self.assertEqual(sorted(sw.stack_problems(Path("/stack"), run)), ["seerr:patch", "service:sonarr"])

    def test_undecodable_output_is_one_problem_not_a_failed_run(self):
        """run_cmd decodes as text, and UnicodeDecodeError is a ValueError, not a COMMAND_ERROR."""
        garbled = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sw.seerr_patch_problem(seerr_stack(tracker=garbled), "c0ffee"),
                             "seerr: can't check its download-tracker patch (details in the journal)")

    def test_a_stack_without_seerr_is_not_asked_about_it(self):
        config = json.dumps({"name": "proj", "services": {"sonarr": {}}})
        run = seerr_stack(container("sonarr"), container("seerr"), tracker=UNPATCHED_TRACKER, config=config)
        self.assertEqual(sw.stack_problems(Path("/stack"), run), {})
        self.assertEqual(execs(run), [])

    def test_the_names_match_the_compose_file_and_the_patch_script(self):
        """The check is asked only when the Compose project has this service, and reads the path the
        build edits. A renamed service would skip it silently; a moved path reports "can't check" on
        every run. Neither should get that far."""
        repo = Path(sw.__file__).resolve().parent.parent
        compose = (repo / "docker-compose.yml").read_text()
        self.assertRegex(compose, rf"(?m)^  {sw.SEERR_SERVICE}:\n")
        script = (repo / "images" / "seerr" / "patch-downloadtracker.sh").read_text()
        self.assertEqual(re.search(r"^target=(\S+)$", script, re.MULTILINE)[1], sw.SEERR_TRACKER)


def free_space(*known):
    """A fake filesystem_free: (path, (device, bytes free)) pairs, or (device, free, total) to say
    how big the filesystem is. Any other path is unreadable. Defaults to a drive comfortably larger
    than any threshold used here, so only the tests that care about size have to say so."""
    paths = dict(known)

    def reader(path):
        if path not in paths:
            raise OSError(2, "No such file or directory")
        answer = paths[path]
        return answer if len(answer) == 3 else (*answer, 4096 * GB)
    return reader


GB = 1024 ** 3
DISK_CFG = {"MEDIA_ROOT": "/lib", "SABNZBD_TEMP": "/dl"}


class Drive:
    """A fake filesystem_free for one filesystem whose free space the test moves between checks.

    Only `at` is readable, so a test that names one role really watches one role: SABNZBD_TEMP falls
    back to Compose's default when it isn't configured, and a drive that answered for every path
    would quietly pull that second role into every test here."""

    def __init__(self, free, at="/lib", total=4096 * GB):
        self.free, self.at, self.total = free, at, total

    def __call__(self, path, timeout=None):
        if path != self.at:
            raise OSError(2, "No such file or directory")
        return 1, self.free, self.total


@contextlib.contextmanager
def mounted(drive):
    original = sw.filesystem_free
    sw.filesystem_free = drive
    try:
        yield drive
    finally:
        sw.filesystem_free = original


class DiskTest(unittest.TestCase):
    def problems(self, minimum, library, downloads, floors=None):
        reader = free_space(("/lib", library), ("/dl", downloads))
        return sw.disk_problems(DISK_CFG, minimum, {} if floors is None else floors, reader)

    def test_quiet_while_there_is_room(self):
        self.assertEqual(self.problems(100 * GB, (1, 143 * GB), (1, 143 * GB)), {})
        self.assertEqual(self.problems(100 * GB, (1, 100 * GB), (1, 100 * GB)), {})

    def test_paths_that_share_a_filesystem_are_one_problem(self):
        """Keyed per role so that splitting them onto separate drives later doesn't retire a key and
        invent two (see the migration test below), but worded once and — because format_check lists
        distinct descriptions — shown as one problem, not two."""
        shared = "disk: dropped below 50G free for the library and downloads"
        problems = self.problems(100 * GB, (1, 30 * GB), (1, 30 * GB))
        self.assertEqual(problems, {"disk:library": shared, "disk:downloads": shared})
        self.assertEqual(sw.format_check(problems, "h", False)[:2],
                         ("Media stack on h: 1 problem", f"• {shared}"))

    def test_separate_filesystems_are_reported_separately(self):
        self.assertEqual(self.problems(100 * GB, (1, 30 * GB), (2, 500 * GB)),
                         {"disk:library": "disk: dropped below 50G free for the library"})
        self.assertEqual(self.problems(100 * GB, (1, 500 * GB), (2, 5 * GB)),
                         {"disk:downloads": "disk: dropped below 10G free for the downloads"})

    def test_it_reports_the_lowest_step_it_is_under(self):
        """The exact figure would differ on nearly every run, and republish the stack notification
        every hour as downloads nudged it. A step only changes when crossing one, which is news."""
        under = {free: self.problems(100 * GB, (1, free), (1, free))["disk:library"]
                 .removeprefix("disk: dropped below ").removesuffix(" free for the library and downloads")
                 for free in (99 * GB, 50 * GB, 50 * GB - 1, 25 * GB, 25 * GB - 1,
                              10 * GB, 10 * GB - 1, 0)}
        self.assertEqual(list(under.values()),
                         ["100G", "100G", "50G", "50G", "25G", "25G", "10G", "10G"])

    def test_the_lowest_step_is_a_floor_not_a_promise_of_more(self):
        """Under the smallest step there is nothing smaller to report, so the text stops changing."""
        self.assertEqual(self.problems(100 * GB, (1, 1), (1, 1))["disk:library"],
                         "disk: dropped below 10G free for the library and downloads")

    def test_a_path_that_is_not_set_is_not_checked(self):
        reader = free_space(("/dl", (1, 5 * GB)))
        self.assertEqual(sw.disk_problems({"SABNZBD_TEMP": "/dl"}, 100 * GB, {}, reader),
                         {"disk:downloads": "disk: dropped below 10G free for the downloads"})
        # MEDIA_ROOT is blank and has no Compose fallback, so nothing is watched; SABNZBD_TEMP's
        # fallback isn't there either, which this reader says by raising.
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sw.disk_problems({"MEDIA_ROOT": "  "}, 100 * GB, {}, reader), {})

    def test_an_unreadable_path_is_reported_not_raised(self):
        reader = free_space(("/dl", (1, 500 * GB)))
        with contextlib.redirect_stderr(io.StringIO()):
            problems = sw.disk_problems(DISK_CFG, 100 * GB, {}, reader)
        self.assertEqual(problems, {"disk:library": "disk: can't read free space for the library path"})

    def test_an_unreadable_path_never_carries_the_os_error_text(self):
        def reader(path):
            raise OSError(13, "SENTINEL_SECRET")
        with contextlib.redirect_stderr(io.StringIO()):
            problems = sw.disk_problems(DISK_CFG, 100 * GB, {}, reader)
        self.assertNotIn("SENTINEL", json.dumps(problems))

    def test_bad_setting_is_rejected_in_words_the_script_wrote(self):
        for text in ("lots", "0G", "100", "500M"):
            with self.assertRaises(sw.WatchError) as caught:
                sw.disk_minimum({"DISK_FREE_MIN": text})
            self.assertIn("DISK_FREE_MIN", str(caught.exception))
        self.assertEqual(sw.disk_minimum({"DISK_FREE_MIN": "100G"}), 100 * GB)
        self.assertEqual(sw.disk_minimum({"DISK_FREE_MIN": "1G"}), GB)

    def test_a_blank_setting_means_the_default_and_only_off_turns_it_off(self):
        """.env.example ships the key blank, so blank must not be read as "no disk check"."""
        self.assertEqual(sw.disk_minimum({}), sw.parse_size(sw.DISK_FREE_MIN_DEFAULT))
        self.assertEqual(sw.disk_minimum({"DISK_FREE_MIN": ""}), sw.parse_size("100G"))
        self.assertEqual(sw.disk_minimum({"DISK_FREE_MIN": "   "}), sw.parse_size("100G"))
        for off in ("off", "OFF", "Off", " off "):
            self.assertIsNone(sw.disk_minimum({"DISK_FREE_MIN": off}), off)

    def test_the_report_only_ever_ratchets_down(self):
        """Free space crossing back up over a step must not change the text: the claim is about the
        low point reached, not the moment, so coming back up doesn't falsify it and nothing is
        republished. Without this a wobble across a step burns the daily cap on alternation."""
        floors = {}

        def level(free):
            return (self.problems(100 * GB, (1, free), (1, free), floors)["disk:library"]
                    .removeprefix("disk: dropped below ").split(" free")[0])
        self.assertEqual([level(free) for free in
                          (60 * GB, 49 * GB, 60 * GB, 99 * GB, 20 * GB, 60 * GB, 5 * GB, 99 * GB)],
                         ["100G", "50G", "50G", "50G", "25G", "25G", "10G", "10G"])
        self.assertEqual(floors, {"disk:library": {"device": 1, "level": 10 * GB},
                                  "disk:downloads": {"device": 1, "level": 10 * GB}})

    def test_retuning_the_threshold_never_claims_a_level_the_drive_was_not_under(self):
        """The floor is a level in bytes, not a fraction, so lowering DISK_FREE_MIN mid-problem
        cannot rescale it into a claim that was never true."""
        floors = {}
        self.problems(100 * GB, (1, 49 * GB), (1, 49 * GB), floors)
        self.assertEqual(floors, {"disk:library": {"device": 1, "level": 50 * GB},
                                  "disk:downloads": {"device": 1, "level": 50 * GB}})
        text = self.problems(60 * GB, (1, 45 * GB), (1, 45 * GB), floors)["disk:library"]
        self.assertEqual(text, "disk: dropped below 50G free for the library and downloads")

    def test_the_default_leaves_room_to_act_above_sabnzbds_own_floor(self):
        """SABnzbd pauses at download_free (50G as shipped) without telling anyone why. The default
        has to clear that by more than the largest season measured on this stack's profile (27G)."""
        self.assertGreaterEqual(sw.parse_size(sw.DISK_FREE_MIN_DEFAULT), sw.parse_size("50G") + 27 * GB)

    def test_an_unset_downloads_path_still_watches_the_one_compose_uses(self):
        """docker-compose.yml mounts ${SABNZBD_TEMP:-/tmp/sabnzbd-temp}, so leaving the setting blank
        doesn't mean no downloads filesystem — it means that one, often a tmpfs. Watching nothing
        would leave SABnzbd's own download_free governing a filesystem this check never looks at."""
        reader = free_space(("/tmp/sabnzbd-temp", (2, 5 * GB)))
        self.assertEqual(sw.disk_problems({"SABNZBD_TEMP": ""}, 100 * GB, {}, reader),
                         {"disk:downloads": "disk: dropped below 10G free for the downloads"})

    def test_the_fallbacks_are_exactly_the_ones_docker_compose_applies(self):
        """Both directions. MEDIA_ROOT is a bare ${MEDIA_ROOT} in Compose, so inventing a fallback
        for it would mean an unset one silently watched some other filesystem and called it the
        library."""
        compose = (Path(sw.__file__).parent.parent / "docker-compose.yml").read_text()
        fallbacks = {key: fallback for key, _, fallback in sw.DISK_ROLES}
        self.assertEqual(fallbacks["SABNZBD_TEMP"],
                         re.search(r"\$\{SABNZBD_TEMP:-([^}]+)\}", compose)[1])
        self.assertIsNone(fallbacks["MEDIA_ROOT"])
        self.assertNotIn("${MEDIA_ROOT:-", compose)

    def test_a_fallback_path_that_is_not_there_is_not_a_problem(self):
        """A configured path that can't be read is the user's claim about their disk, so it is
        reported. The fallback is this script's own inference and Compose only creates it on `up`, so
        an absent one is neither an alert about a directory nobody asked for nor a journal line every
        fifteen minutes for as long as the setting stays blank."""
        reader = free_space(("/lib", (1, 500 * GB)))
        with contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertEqual(sw.disk_problems({"MEDIA_ROOT": "/lib"}, 100 * GB, {}, reader), {})
        self.assertEqual(log.getvalue(), "")

    def test_a_fallback_that_goes_quiet_mid_problem_is_unknown_not_recovered(self):
        """The same absent path as above, once it is already a problem, is a different fact. Skipping
        it there drops the problem, advance() decays it, and the all clear names it as "Cleared" — a
        recovery announced on the strength of the path having stopped answering, which is as close to
        a missed alert as this check gets. A floor is exactly the roles that were a problem last run,
        so it is what tells the two apart."""
        reader = free_space(("/lib", (1, 500 * GB)))
        floors = {"disk:downloads": {"device": 2, "level": 10 * GB}}
        with contextlib.redirect_stderr(io.StringIO()) as log:
            problems = sw.disk_problems({"MEDIA_ROOT": "/lib"}, 100 * GB, floors, reader)
        self.assertEqual(problems,
                         {"disk:downloads": "disk: can't read free space for the downloads path"})
        self.assertIn("SABNZBD_TEMP", log.getvalue())  # and now it is worth a journal line
        self.assertEqual(floors["disk:downloads"], {"device": 2, "level": 10 * GB})

    def test_the_current_figure_is_collected_beside_the_ratcheted_one(self):
        """Separately from the description, which is the whole point: the ratchet exists because free
        space is the one quantity here that varies continuously, and a varying description republishes
        the stack notification. Collected per role, so two roles on one drive still say one thing."""
        floors, free_now = {}, {}
        self.problems(100 * GB, (1, 5 * GB), (1, 5 * GB), floors)
        settled = self.problems(100 * GB, (1, 70 * GB), (1, 70 * GB), floors)
        sw.disk_problems(DISK_CFG, 100 * GB, dict(floors),
                         free_space(("/lib", (1, 70 * GB)), ("/dl", (1, 70 * GB))), free_now)
        self.assertEqual(set(settled.values()),  # the sentence has not moved: 10G, as it should
                         {"disk: dropped below 10G free for the library and downloads"})
        self.assertEqual(free_now, {"disk:library": 70 * GB, "disk:downloads": 70 * GB})

    def test_moving_downloads_to_its_own_drive_keeps_the_librarys_key_and_floor(self):
        """The key used to be built from the role set, so disk:library+downloads retired and two new
        keys appeared the moment the paths split: a spurious rotating_light "new problem", a floor
        with nothing to belong to, and a report back at the top step. docs/sabnzbd.md recommends
        exactly that split, so this is a migration this host is being pointed at."""
        floors = {}
        self.assertEqual(self.problems(100 * GB, (1, 20 * GB), (1, 20 * GB), floors).keys(),
                         {"disk:library", "disk:downloads"})
        self.assertEqual(floors, {"disk:library": {"device": 1, "level": 25 * GB},
                                  "disk:downloads": {"device": 1, "level": 25 * GB}})
        split = self.problems(100 * GB, (1, 20 * GB), (2, 5 * GB), floors)
        self.assertEqual(split, {"disk:library": "disk: dropped below 25G free for the library",
                                 "disk:downloads": "disk: dropped below 10G free for the downloads"})
        self.assertEqual(floors, {"disk:library": {"device": 1, "level": 25 * GB},
                                  "disk:downloads": {"device": 2, "level": 10 * GB}})

    def test_filesystems_merging_mid_problem_still_read_as_one_sentence(self):
        """The mirror of the split: downloads joins the drive the library is already on. Its old
        drive's low point is not the new drive's, so it is dropped, and the floor already held for
        this device governs both roles — one sentence, not two bullets for one drive claiming two
        different low points."""
        floors = {"disk:library": {"device": 1, "level": 50 * GB},
                  "disk:downloads": {"device": 2, "level": 25 * GB}}
        merged = self.problems(100 * GB, (1, 40 * GB), (1, 40 * GB), floors)
        self.assertEqual(set(merged.values()),
                         {"disk: dropped below 50G free for the library and downloads"})
        self.assertEqual(floors, {"disk:library": {"device": 1, "level": 50 * GB},
                                  "disk:downloads": {"device": 1, "level": 50 * GB}})

    def test_a_wedged_filesystem_is_given_up_on_rather_than_blocking_the_run(self):
        """os.statvfs and os.stat were the only external calls here with no timeout of their own, and
        they block uninterruptibly on a spun-down, failing or stale mount. The check runs after the
        containers and Jellyfin, so TimeoutStartSec=5min would SIGTERM the unit with those results
        gathered but unsaved, no failure recorded, and nothing heard until the heartbeat."""
        started = threading.Event()

        def wedged(path):
            started.set()
            time.sleep(30)  # abandoned; a daemon thread doesn't hold up interpreter exit
        began = time.monotonic()
        with self.assertRaises(TimeoutError):
            sw.filesystem_free("/lib", timeout=0.2, read=wedged)
        self.assertLess(time.monotonic() - began, 5)
        self.assertTrue(started.is_set())

    def test_the_abandoned_read_runs_on_a_daemon_thread(self):
        """daemon=True is the whole safety of abandoning the read rather than cancelling it — a
        thread stuck in a syscall cannot be cancelled, and a non-daemon one is joined at interpreter
        shutdown, which is precisely the TimeoutStartSec SIGTERM this was written to prevent.
        Measured separately: the same script exits in ~0.4s with daemon=True and hangs without it."""
        release = threading.Event()
        before = set(threading.enumerate())
        with self.assertRaises(TimeoutError):
            sw.filesystem_free("/lib", timeout=0.2, read=lambda path: release.wait(30))
        [abandoned] = [t for t in threading.enumerate() if t not in before]
        try:
            self.assertTrue(abandoned.daemon)
        finally:
            release.set()
            abandoned.join(5)

    def test_a_floor_does_not_follow_a_role_onto_a_different_drive(self):
        """The floor belongs to the filesystem, not to the role. Keyed by role alone it would move
        with MEDIA_ROOT onto a bigger drive and go on reporting a level that drive has never been
        under — and because the description wouldn't change, publish_stack would never republish it,
        so the falsehood would stand silently for as long as the problem lasted."""
        floors = {}
        self.problems(100 * GB, (1, 5 * GB), (1, 5 * GB), floors)
        self.assertEqual(floors["disk:library"], {"device": 1, "level": 10 * GB})
        moved = self.problems(100 * GB, (9, 60 * GB), (1, 5 * GB), floors)
        self.assertEqual(moved["disk:library"], "disk: dropped below 100G free for the library")
        self.assertEqual(floors["disk:library"], {"device": 9, "level": 100 * GB})
        self.assertEqual(floors["disk:downloads"], {"device": 1, "level": 10 * GB})

    def test_a_floor_inherited_without_a_device_is_honoured_once_then_stamped(self):
        """Upgrading mustn't throw away a low point already reported; migrate_disk_keys leaves the
        level with no device, and the first run that reads it learns which drive it was on."""
        floors = {"disk:library": {"level": 10 * GB}, "disk:downloads": {"level": 10 * GB}}
        text = self.problems(100 * GB, (4, 60 * GB), (4, 60 * GB), floors)["disk:library"]
        self.assertEqual(text, "disk: dropped below 10G free for the library and downloads")
        self.assertEqual(floors["disk:library"], {"device": 4, "level": 10 * GB})

    def test_a_readable_filesystem_is_not_slowed_down_by_the_timeout(self):
        began = time.monotonic()
        self.assertEqual(sw.filesystem_free("/lib", read=lambda path: (7, 3 * GB, 9 * GB)),
                         (7, 3 * GB, 9 * GB))
        with self.assertRaises(OSError) as caught:  # a real error still arrives as itself
            sw.filesystem_free("/lib", read=raises(OSError(2, "No such file or directory")))
        self.assertEqual(caught.exception.errno, 2)
        self.assertLess(time.monotonic() - began, 1)  # the name promises this; assert it

    def test_a_fallback_filesystem_too_small_for_the_threshold_is_not_watched(self):
        """Compose's fallback is usually /tmp, a tmpfs of a few gigabytes, and DISK_FREE_MIN is sized
        for the media library. Watching it would be a standing alert nobody can ever clear — on the
        same ntfy sequence_id as the real library-full alert, so it would train the artifex to swipe
        away the one notification this whole check exists to deliver."""
        reader = free_space(("/lib", (1, 500 * GB)), ("/tmp/sabnzbd-temp", (2, 5 * GB, 16 * GB)))
        with contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertEqual(sw.disk_problems({"MEDIA_ROOT": "/lib"}, 100 * GB, {}, reader), {})
        self.assertIn("SABNZBD_TEMP", log.getvalue())

    def test_a_configured_filesystem_too_small_for_the_threshold_is_still_the_users_claim(self):
        """Only the inferred fallback is second-guessed. A path the user named is watched as asked."""
        reader = free_space(("/dl", (1, 5 * GB, 16 * GB)))
        self.assertEqual(sw.disk_problems({"SABNZBD_TEMP": "/dl"}, 100 * GB, {}, reader),
                         {"disk:downloads": "disk: dropped below 10G free for the downloads"})

    def test_a_timed_out_filesystem_reads_as_unreadable_and_leaks_no_path(self):
        def wedged(path, timeout=None):
            raise TimeoutError(f"reading {path} took longer than 20s")
        with contextlib.redirect_stderr(io.StringIO()):
            problems = sw.disk_problems({"MEDIA_ROOT": "/SENTINEL_HOME/media"}, 100 * GB, {}, wedged)
        self.assertEqual(problems, {"disk:library": "disk: can't read free space for the library path"})
        self.assertNotIn("SENTINEL", json.dumps(problems))


class AdvanceTest(unittest.TestCase):
    @staticmethod
    def step(tracked, present, at):
        problems = {"a": "a: exited"} if present else {}
        return sw.advance(tracked, problems, NOW + at, sw.REAL_TIMING.problem_age)

    @staticmethod
    def alerted(tracked):
        return tracked.get("a", {}).get("alerted", False)

    def test_alerts_on_the_second_run(self):
        tracked = self.step({}, True, 0 * MIN)
        self.assertEqual((tracked["a"]["count"], self.alerted(tracked)), (1, False))
        tracked = self.step(tracked, True, 15 * MIN)
        self.assertEqual(tracked["a"], {"count": 2, "first": NOW.isoformat(), "seen": (NOW + 15 * MIN).isoformat(),
                                        "alerted": True, "description": "a: exited", "absent": 0})

    def test_runs_seconds_apart_after_a_wake_do_not_alert(self):
        tracked = self.step(self.step({}, True, 0 * MIN), True, 50 * SEC)
        self.assertEqual((tracked["a"]["count"], self.alerted(tracked)), (2, False))
        self.assertTrue(self.alerted(self.step(tracked, True, 15 * MIN)))

    def test_problem_must_span_ten_minutes(self):
        self.assertFalse(self.alerted(self.step(self.step({}, True, 0 * MIN), True, 10 * MIN - SEC)))
        self.assertTrue(self.alerted(self.step(self.step({}, True, 0 * MIN), True, 10 * MIN)))

    def test_one_missed_run_still_counts_two_do_not(self):
        self.assertEqual(sw.CHECK_INTERVAL, 15 * MIN)
        self.assertTrue(self.alerted(self.step(self.step({}, True, 0 * MIN), True, 30 * MIN)))
        tracked = self.step(self.step({}, True, 0 * MIN), True, 45 * MIN)
        self.assertEqual((tracked["a"]["count"], self.alerted(tracked)), (1, False))

    def test_all_clear_needs_two_runs_without_the_problem(self):
        tracked = self.step(self.step({}, True, 0 * MIN), True, 15 * MIN)
        tracked = self.step(tracked, False, 30 * MIN)
        self.assertEqual((self.alerted(tracked), tracked["a"]["absent"]), (True, 1))
        self.assertEqual(self.step(tracked, False, 45 * MIN), {})

    def test_problem_back_before_clearing_restarts_the_count(self):
        tracked = self.step(self.step({}, True, 0 * MIN), True, 15 * MIN)
        for minutes, present in ((30, False), (45, True), (60, False)):
            tracked = self.step(tracked, present, minutes * MIN)
        self.assertEqual((self.alerted(tracked), tracked["a"]["absent"]), (True, 1))

    def test_blip_that_clears_before_alerting_is_forgotten(self):
        self.assertEqual(self.step(self.step({}, True, 0 * MIN), False, 15 * MIN), {})

    def test_alerted_problem_stays_alerted_across_a_gap(self):
        tracked = self.step(self.step({}, True, 0 * MIN), True, 15 * MIN)
        tracked = self.step(tracked, True, 10 * HOUR)
        self.assertEqual((tracked["a"]["count"], self.alerted(tracked)), (3, True))

    def test_upgrades_entries_without_first_seen(self):
        old = {"a": {"count": 1, "alerted": False, "description": "x", "seen": (NOW - 15 * MIN).isoformat()}}
        self.assertTrue(sw.advance(old, {"a": "x"}, NOW, 10 * MIN)["a"]["alerted"])


class PublishStackTest(unittest.TestCase):
    def publish(self, current, stack, now=NOW):
        self.http = FakeHttp()
        tracked = {k: {"alerted": True, "description": d} for k, d in current.items()}
        tracked["quiet"] = {"alerted": False, "description": "quiet: not yet alerted"}
        return sw.publish_stack(make_watch(http=self.http, now=now), tracked, stack), self.http.published()

    def test_new_problem_goes_out_at_once(self):
        stack, sent = self.publish({"a": "a: exited"}, {"shown": {}, "sent": [(NOW - MIN).isoformat()]})
        self.assertEqual([(m["title"], m["message"], m["priority"], m["sequence_id"], m["tags"]) for m in sent],
                         [("Media stack on h: 1 problem", "• a: exited", 4, "h-stack", ["rotating_light"])])
        self.assertEqual(stack, {"shown": {"a": "a: exited"}, "reported": {"a": NOW.isoformat()},
                                 "sent": [(NOW - MIN).isoformat(), NOW.isoformat()]})

    def test_unchanged_sends_nothing(self):
        stack = {"shown": {"a": "a: exited"}, "sent": [(NOW - MIN).isoformat()]}
        self.assertEqual(self.publish({"a": "a: exited"}, stack), (stack, []))

    def test_all_clear_waits_an_hour_after_the_last_update(self):
        stack = {"shown": {"a": "a: exited"}, "sent": [(NOW - 59 * MIN).isoformat()]}
        self.assertEqual(self.publish({}, stack), (stack, []))
        new, sent = self.publish({}, stack, now=NOW + MIN)
        self.assertEqual([(m["title"], m["message"], m["priority"], m["tags"]) for m in sent],
                         [("Media stack on h: all clear",
                           "Everything that was down is back up.\n\nCleared:\n• a: exited", 2,
                           ["white_check_mark"])])
        self.assertEqual(new["shown"], {})

    def test_a_changed_description_counts_as_worse(self):
        """This used to go out at Default: a description change was not counted as worsening. Every
        description moves between bad states — restarting to exited, a disk step down — never towards
        good, and since the disk check ratchets, a changed description is its only escalation."""
        stack = {"shown": {"a": "a: restarting", "b": "b: unhealthy"}, "sent": []}
        _, sent = self.publish({"a": "a: exited (code 1)", "b": "b: unhealthy"}, stack)
        self.assertEqual([(m["title"], m["message"], m["priority"], m["tags"]) for m in sent],
                         [("Media stack on h: 2 problems", "• a: exited (code 1)\n• b: unhealthy",
                           sw.HIGH, ["rotating_light"])])

    def test_a_problem_clearing_is_still_not_worse(self):
        """Only a new key or a changed one raises the priority; losing a key on its own stays at
        Default. The tag follows the state rather than the transition, though: a notification still
        titled "1 problem" must not carry a check mark, which is the shape of bug that sent "dropped
        below 25G" out as good news."""
        stack = {"shown": {"a": "a: exited (code 1)", "b": "b: unhealthy"}, "sent": []}
        _, sent = self.publish({"a": "a: exited (code 1)"}, stack)
        self.assertEqual([(m["priority"], m["tags"]) for m in sent], [(sw.DEFAULT, ["warning"])])

    def test_a_standing_problem_is_re_sent_once_a_day(self):
        """Without this a problem is announced exactly once, ever: the text settles — the ratchet
        makes it settle sooner — publish_stack short-circuits on current == shown, and a drive parked
        at 5G free is one notification that may have been swiped away weeks ago, with no all clear
        coming until somebody acts on it."""
        shown = {"disk:library": "disk: dropped below 10G free for the library"}
        for hours, expected in ((23, 0), (sw.STACK_REPEAT // HOUR, 1)):
            with self.subTest(hours=hours):
                stack = {"shown": dict(shown), "sent": [(NOW - hours * HOUR).isoformat()],
                         "reported": {"disk:library": (NOW - hours * HOUR).isoformat()}}
                new, sent = self.publish(dict(shown), stack)
                self.assertEqual(len(sent), expected)
                self.assertEqual(new["shown"], shown)

    def test_a_repeat_says_it_is_unresolved_rather_than_unchanged_and_is_not_good_news(self):
        """"Unchanged" would be a claim about the world, and a repeat can reach someone who has just
        changed a great deal: the disk report ratchets to the episode's low point, so freeing 60G to
        go from 10G to 70G leaves the problem and the sentence exactly as they were. "Still not
        resolved" is a claim about the problem, which is the one that is actually true."""
        shown = {"disk:library": "disk: dropped below 10G free for the library"}
        stack = {"shown": dict(shown), "sent": [(NOW - 2 * DAY).isoformat()]}
        self.http = FakeHttp()
        tracked = {"disk:library": {"alerted": True, "description": shown["disk:library"],
                                    "first": (NOW - 3 * DAY).isoformat()}}
        sw.publish_stack(make_watch(http=self.http), tracked, stack)
        [sent] = self.http.published()
        self.assertEqual((sent["title"], sent["priority"], sent["tags"]),
                         ("Media stack on h: 1 problem", sw.DEFAULT, ["warning"]))
        self.assertEqual(sent["message"], f"• {shown['disk:library']}\n\nStill not resolved. "
                                          f"First seen {sw.when(NOW - 3 * DAY)}.")

    def test_first_seen_names_the_oldest_problem_in_the_set(self):
        """min, not max: with a disk problem three weeks old beside a container that died yesterday,
        the set has been there three weeks."""
        shown = {"a": "a: exited", "b": "b: unhealthy"}
        self.http = FakeHttp()
        sw.publish_stack(make_watch(http=self.http), {
            "a": {"alerted": True, "description": "a: exited", "first": (NOW - 21 * DAY).isoformat()},
            "b": {"alerted": True, "description": "b: unhealthy", "first": (NOW - DAY).isoformat()},
        }, {"shown": dict(shown), "sent": [(NOW - 2 * DAY).isoformat()]})
        self.assertIn(f"First seen {sw.when(NOW - 21 * DAY)}.", self.http.published()[0]["message"])

    def test_a_problem_on_its_way_out_is_not_re_sent_as_still_not_resolved(self):
        """advance holds a problem alerted through one absent run, so `current` can still name
        something that has just gone. Re-sending "still not resolved" about it would be the one case
        where the re-send makes a false claim about the world — and the all clear is one run behind
        it anyway."""
        self.http = FakeHttp()
        stack = {"shown": {"a": "a: exited"}, "sent": [(NOW - 2 * DAY).isoformat()]}
        new = sw.publish_stack(make_watch(http=self.http), {
            "a": {"alerted": True, "description": "a: exited", "absent": 1,
                  "first": (NOW - 3 * DAY).isoformat()}}, stack)
        self.assertEqual((self.http.published(), new), ([], stack))

    def test_raising_the_repeat_interval_actually_raises_it(self):
        """`sent` used to be pruned at a day flat, so the STACK_REPEAT comparison could never be the
        reason a re-send fired — it always went through the empty-list branch, and the constant was
        decoration. A test reading sw.STACK_REPEAT passed for any value, which is the same
        right-outcome-wrong-mechanism shape as the cap test this replaced."""
        shown = {"a": "a: exited"}
        stack = {"shown": dict(shown), "sent": [NOW.isoformat()]}
        with unittest.mock.patch.object(sw, "STACK_REPEAT", 7 * DAY):
            self.assertEqual(self.publish(dict(shown), stack, now=NOW + 3 * DAY)[1], [])
            self.assertEqual(len(self.publish(dict(shown), stack, now=NOW + 7 * DAY)[1]), 1)

    def test_a_repeat_of_state_too_old_to_know_when_it_started_still_says_it_is_a_repeat(self):
        """A stack record written before this change has no "first", and a bare re-send of the same
        text would read as fresh news."""
        shown = {"a": "a: exited"}
        self.http = FakeHttp()
        sw.publish_stack(make_watch(http=self.http), {"a": {"alerted": True, "description": "a: exited"}},
                         {"shown": dict(shown), "sent": [(NOW - 2 * DAY).isoformat()]})
        [sent] = self.http.published()
        self.assertEqual(sent["message"], "• a: exited\n\nStill not resolved.")

    def test_a_repeat_costs_at_most_one_message_a_day_by_construction(self):
        """Not by STACK_DAILY_CAP, which can never mute a repeat: `sent` is pruned to the last day,
        a repeat needs its newest entry to be at least a day old, so `sent` is empty and far under
        the cap whenever one is due. The bound comes from publishing resetting that same clock."""
        shown = {"a": "a: exited"}
        stack = {"shown": dict(shown), "sent": [(NOW - 2 * DAY).isoformat()]}
        new, sent = self.publish(dict(shown), stack)
        self.assertEqual(len(sent), 1)
        self.assertEqual(new["sent"], [NOW.isoformat()])  # the stale entry pruned, the clock reset
        self.assertEqual(self.publish(dict(shown), new, now=NOW + 23 * HOUR)[1], [])
        self.assertEqual(len(self.publish(dict(shown), new, now=NOW + DAY)[1]), 1)

    def test_the_all_clear_names_what_recovered(self):
        """Ratcheted, the report doesn't climb back as space is freed, so deleting 60G to go from
        10G to 70G free is acknowledged by nothing at all until DISK_FREE_MIN is cleared. The all
        clear is that acknowledgement, and "everything that was down is back up" never said what."""
        one_drive = "disk: dropped below 10G free for the library and downloads"
        stack = {"shown": {"disk:library": one_drive, "disk:downloads": one_drive, "a": "a: exited"},
                 "sent": []}
        _, sent = self.publish({}, stack)
        self.assertEqual([(m["title"], m["message"], m["tags"]) for m in sent], [
            ("Media stack on h: all clear",
             f"Everything that was down is back up.\n\nCleared:\n• a: exited\n• {one_drive}",
             ["white_check_mark"])])

    def test_a_recovery_is_named_while_other_problems_remain(self):
        """The all clear used to be the only place a recovery was named, so a partial one was erased:
        the bullet simply stopped appearing, in a notification whose title changed from "3 problems"
        to "2 problems" and said nothing about which. The disk is why this matters most — its report
        ratchets, so a recovery is acknowledged by nothing else at all — but a container coming back
        while another is still down is the same silence."""
        stack = {"shown": {"a": "a: exited", "b": "b: unhealthy",
                           "disk:library": "disk: dropped below 10G free for the library"},
                 "sent": []}
        _, sent = self.publish({"a": "a: exited"}, stack)
        self.assertEqual([(m["title"], m["message"], m["priority"], m["tags"]) for m in sent], [
            ("Media stack on h: 1 problem",
             "• a: exited\n\nCleared:\n• b: unhealthy\n"
             "• disk: dropped below 10G free for the library", sw.DEFAULT, ["warning"])])

    def test_one_drive_shared_by_both_roles_recovers_as_one_line(self):
        """As in the all clear: the check keys per role and words a shared drive once, so listing per
        key would name the same drive twice in the same breath."""
        one_drive = "disk: dropped below 10G free for the library and downloads"
        stack = {"shown": {"disk:library": one_drive, "disk:downloads": one_drive,
                           "a": "a: exited"}, "sent": []}
        _, sent = self.publish({"a": "a: exited"}, stack)
        self.assertEqual([m["message"] for m in sent], [f"• a: exited\n\nCleared:\n• {one_drive}"])

    def test_nothing_is_added_when_a_problem_set_only_grows(self):
        """A new problem beside an old one has nothing to acknowledge, and an empty "Cleared:" header
        would be worse than none."""
        stack = {"shown": {"a": "a: exited"}, "sent": []}
        _, sent = self.publish({"a": "a: exited", "b": "b: unhealthy"}, stack)
        self.assertEqual([m["message"] for m in sent], ["• a: exited\n• b: unhealthy"])

    def test_a_ratcheted_disk_sentence_quotes_what_the_drive_has_now(self):
        """The sentence is held at the episode's low point so it stops republishing, which means a
        re-send a day later can describe 10G to someone now sitting at 70G and read as a claim about
        the present. The figure is carried beside it rather than in it: in it, the description would
        change on nearly every run and the flap the ratchet removed would be back."""
        one_drive = "disk: dropped below 10G free for the library and downloads"
        current = {"disk:library": one_drive, "disk:downloads": one_drive}
        title, message, _ = sw.format_check(current, "h", False, free_now={
            "disk:library": 70 * GB, "disk:downloads": 70 * GB})
        self.assertEqual((title, message),
                         ("Media stack on h: 1 problem", f"• {one_drive} (now 70G free)"))

    def test_daily_cap_mutes_updates_until_a_day_after_the_oldest(self):
        times = [NOW - 23 * HOUR + n * MIN for n in range(11)] + [NOW - 25 * HOUR]
        stack, sent = self.publish({"a": "a: exited"}, {"shown": {}, "sent": [t.isoformat() for t in times]})
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["message"].endswith(
            f"This has changed 12 times in a day, so updates are muted until {sw.when(NOW + HOUR)}, "
            "except new problems."))
        self.assertEqual((len(stack["sent"]), stack["reported"]), (12, {"a": NOW.isoformat()}))
        muted = dict(stack, shown={})  # "a" cleared: a flapper coming back stays muted
        self.assertEqual(self.publish({"a": "a: exited"}, muted, now=NOW + 30 * MIN), (muted, []))
        self.assertEqual(len(self.publish({"a": "a: exited"}, muted, now=NOW + HOUR + MIN)[1]), 1)

    def test_a_problem_not_reported_today_gets_through_the_cap(self):
        """A flapping check must not hide a container that dies for real."""
        times = [(NOW - HOUR + n * MIN).isoformat() for n in range(12)]
        stack = {"shown": {}, "sent": times, "reported": {"a": (NOW - HOUR).isoformat(),
                                                          "b": (NOW - DAY).isoformat()}}
        for key in ("b", "c"):
            with self.subTest(key=key):
                new, sent = self.publish({key: f"{key}: exited"}, stack)
                self.assertEqual(len(sent), 1)
                self.assertIn("muted until", sent[0]["message"])
                self.assertEqual(len(new["sent"]), 13)
        self.assertEqual(self.publish({"a": "a: exited"}, stack), (stack, []))

    def test_failed_send_is_retried_next_run(self):
        stack = {"shown": {}, "sent": []}
        tracked = {"a": {"alerted": True, "description": "a: exited"}}
        with self.assertRaises(urllib.error.URLError):
            sw.publish_stack(make_watch(http=raises(urllib.error.URLError("down"))), tracked, stack)
        self.assertEqual(stack, {"shown": {}, "sent": []})
        self.assertEqual(len(self.publish({"a": "a: exited"}, stack, now=NOW + 15 * MIN)[1]), 1)


class HeartbeatTest(unittest.TestCase):
    def beat(self, grace, heartbeat=None, now=NOW, timing=sw.REAL_TIMING, http=None):
        self.http = FakeHttp() if http is None else http
        self.state = {} if heartbeat is None else {"heartbeat": dict(heartbeat)}
        sw.heartbeat(make_watch({"HEARTBEAT_GRACE": grace}, self.http, now=now, timing=timing), self.state)
        return self.state.get("heartbeat"), self.http.published()

    @staticmethod
    def due_in(delta, last_ago=15 * MIN):
        return {"due": (NOW + delta).isoformat(), "last": (NOW - last_ago).isoformat()}

    def test_first_run_schedules_the_alert_one_step_past_grace(self):
        beat, sent = self.beat("24h")
        self.assertEqual(beat, {"due": (NOW + 25 * HOUR).isoformat(), "last": NOW.isoformat(),
                                "published": NOW.isoformat()})
        self.assertEqual([(m["title"], m["priority"], m["sequence_id"], m["delay"]) for m in sent],
                         [("h is unreachable", 4, "h-heartbeat", str(int((NOW + 25 * HOUR).timestamp())))])
        self.assertEqual(sent[0]["message"],
                         f"Its last check-in was {sw.when(NOW)} or up to 1h later. It is asleep, powered off "
                         "or offline, or the stack watch stopped (systemctl --user list-timers "
                         "'media-stack-*'). Jellyfin and Seerr can't be reached until it's back.")

    def test_moves_the_alert_only_once_a_step_has_passed(self):
        beat, sent = self.beat("24h", self.due_in(24 * HOUR + 2 * MIN + SEC))
        self.assertEqual((sent, beat), ([], {"due": (NOW + 24 * HOUR + 2 * MIN + SEC).isoformat(),
                                             "last": NOW.isoformat()}))
        beat, sent = self.beat("24h", self.due_in(24 * HOUR + 2 * MIN))
        self.assertEqual((len(sent), beat["due"]), (1, (NOW + 25 * HOUR).isoformat()))

    def test_costs_one_message_an_hour_at_any_grace_and_never_alerts_early(self):
        for grace_text, grace in (("30m", 30 * MIN), ("6h", 6 * HOUR), ("24h", DAY), ("70h", 70 * HOUR)):
            with self.subTest(grace=grace_text):
                state, total = {}, 0
                for run in range(4 * 24):
                    now = NOW + run * 15 * MIN + (run % 50) * SEC  # timer jitter
                    http = FakeHttp()
                    sw.heartbeat(make_watch({"HEARTBEAT_GRACE": grace_text}, http, now=now), state)
                    total += len(http.published())
                    self.assertGreater(sw.parse_ts(state["heartbeat"]["due"]), now + grace)
                self.assertEqual(total, 24)

    def test_lowering_grace_reschedules_without_a_false_back_online(self):
        beat, sent = self.beat("1h", self.due_in(24 * HOUR + 30 * MIN))
        self.assertEqual(([m["title"] for m in sent], beat["due"]), (["h is unreachable"], (NOW + 2 * HOUR).isoformat()))

    def test_raising_grace_reschedules_before_the_old_alert_fires(self):
        beat, sent = self.beat("24h", self.due_in(HOUR + 30 * MIN))
        self.assertEqual(([m["title"] for m in sent], beat["due"]), (["h is unreachable"], (NOW + 25 * HOUR).isoformat()))

    def test_back_online_only_once_the_alert_was_delivered(self):
        _, sent = self.beat("24h", self.due_in(MIN))  # still comfortably ahead of ntfy's sender
        self.assertEqual([m["title"] for m in sent], ["h is unreachable"])
        beat, sent = self.beat("24h", self.due_in(-MIN, last_ago=26 * HOUR))
        self.assertEqual([m["title"] for m in sent], ["h is back online", "h is unreachable"])
        self.assertNotIn("delay", sent[0])
        self.assertEqual((sent[0]["priority"], sent[0]["sequence_id"]), (3, "h-heartbeat"))
        self.assertEqual(sent[0]["message"], f"Out of contact from {sw.when(NOW - 26 * HOUR)} to {sw.when(NOW)}. "
                                             f"The unreachable alert went out {sw.when(NOW - MIN)}.")
        self.assertEqual(beat["due"], (NOW + 25 * HOUR).isoformat())

    def test_a_run_racing_ntfys_sender_claims_the_recovery_without_asserting_delivery(self):
        """Within CANCEL_SETTLE of `due` nobody can say whether ntfy released the message yet.

        Staying silent is the worse guess: the reschedule below publishes on the same sequence and
        so replaces a message still waiting, which would strand an "unreachable" the artifex is
        already holding with no all clear ever coming — `due` having moved 25h out, `now > due` is
        never true again. So it is announced, and the sentence hedges the one fact in it that is
        genuinely unknown rather than stating a delivery that may not have happened."""
        for ahead in (SEC, 4 * SEC):
            with self.subTest(ahead=ahead):
                beat, sent = self.beat("24h", self.due_in(ahead, last_ago=26 * HOUR))
                self.assertEqual([m["title"] for m in sent], ["h is back online", "h is unreachable"])
                self.assertEqual(sent[0]["message"],
                                 f"Out of contact from {sw.when(NOW - 26 * HOUR)} to {sw.when(NOW)}. "
                                 f"The unreachable alert was due {sw.when(NOW + ahead)} and may "
                                 "already have gone out.")
                self.assertEqual(beat["due"], (NOW + 25 * HOUR).isoformat())

    def test_back_online_is_not_repeated_when_rescheduling_fails(self):
        with self.assertRaises(urllib.error.URLError):
            self.beat("24h", self.due_in(-MIN, last_ago=26 * HOUR), http=FakeHttp(fail_posts_after=1))
        self.assertEqual([m["title"] for m in self.http.published()], ["h is back online"])
        self.assertEqual(self.state["heartbeat"], {"last": NOW.isoformat()})
        _, sent = self.beat("24h", self.state["heartbeat"], now=NOW + 15 * MIN)
        self.assertEqual([m["title"] for m in sent], ["h is unreachable"])

    def test_failed_back_online_is_retried(self):
        with self.assertRaises(urllib.error.URLError):
            self.beat("24h", self.due_in(-MIN), http=FakeHttp(fail_posts_after=0))
        self.assertEqual(self.state["heartbeat"], self.due_in(-MIN))

    def test_upgrades_state_that_stored_the_publish_time(self):
        _, sent = self.beat("24h", {"armed": (NOW - HOUR).isoformat(), "last": (NOW - HOUR).isoformat()})
        self.assertEqual([m["title"] for m in sent], ["h is unreachable"])
        self.assertEqual(self.state["heartbeat"], {"due": (NOW + 25 * HOUR).isoformat(),
                                                   "last": NOW.isoformat(), "published": NOW.isoformat()})
        _, sent = self.beat("24h", {"armed": (NOW - 25 * HOUR).isoformat(), "last": (NOW - 25 * HOUR).isoformat()})
        self.assertEqual([m["title"] for m in sent], ["h is back online", "h is unreachable"])
        _, sent = self.beat("1h", {"armed": (NOW - 70 * MIN).isoformat(), "last": (NOW - 70 * MIN).isoformat()})
        self.assertEqual([m["title"] for m in sent], ["h is unreachable"])  # lowered in the same edit

    def test_blank_grace_means_default_not_off(self):
        self.assertEqual(self.beat("")[0]["due"], (NOW + 25 * HOUR).isoformat())

    def test_off_cancels_a_scheduled_alert(self):
        for scheduled in (self.due_in(HOUR), {"armed": NOW.isoformat(), "last": NOW.isoformat()}):
            self.assertEqual(self.beat("off", scheduled), (None, []))
            self.assertEqual(self.http.deletes(), ["https://ntfy.test/topic/h-heartbeat"])
        self.assertEqual(self.beat("off"), (None, []))
        self.assertEqual(self.http.calls, [])

    def test_failed_cancel_keeps_the_heartbeat_for_a_retry(self):
        with self.assertRaises(urllib.error.URLError):
            self.beat("off", self.due_in(HOUR), http=raises(urllib.error.URLError("down")))
        self.assertEqual(self.state["heartbeat"], self.due_in(HOUR))

    def test_grace_limits(self):
        for grace in ("29m", "71h", "72h", "90", "SENTINEL_SECRET"):
            with self.subTest(grace=grace), self.assertRaises(sw.WatchError) as caught:
                self.beat(grace)
            self.assertEqual(str(caught.exception), "HEARTBEAT_GRACE must be 30m to 70h, or off")
        for grace in ("30m", "70h"):
            self.assertEqual(len(self.beat(grace)[1]), 1)
        with self.assertRaisesRegex(sw.WatchError, "^HEARTBEAT_GRACE must be 10s to 71h, or off$"):
            self.beat("9s", timing=sw.TEST_TIMING)

    def test_the_longest_grace_still_schedules_inside_ntfys_delay_limit(self):
        """The alert goes out a step past grace, so the ceiling has to leave room for the step AND
        for the two clocks disagreeing. Scheduled at exactly MAX_DELAY ntfy rejects the publish
        outright rather than trimming it, which breaks rescheduling altogether — the dead man's
        switch failing shut, at the setting chosen to make it wait longest."""
        for timing in (sw.REAL_TIMING, sw.TEST_TIMING):
            longest = sw.MAX_DELAY - timing.heartbeat_step - sw.DELAY_MARGIN
            with self.subTest(timing=timing):
                beat, sent = self.beat(sw.span(longest), timing=timing, now=NOW)
                delay = sw.parse_ts(beat["due"]) - NOW
                self.assertEqual(delay, longest + timing.heartbeat_step)
                self.assertLessEqual(delay, sw.MAX_DELAY - sw.DELAY_MARGIN)
                self.assertEqual(sent[0]["delay"], str(int((NOW + delay).timestamp())))

    def test_test_timing_reschedules_every_run_at_exactly_grace(self):
        beat, sent = self.beat("10s", self.due_in(5 * SEC), timing=sw.TEST_TIMING)
        self.assertEqual(sent[0]["delay"], str(int((NOW + 10 * SEC).timestamp())))
        self.assertTrue(sent[0]["message"].startswith(f"Its last check-in was {sw.when(NOW)}. It is"))
        beat, sent = self.beat("71h", timing=sw.TEST_TIMING)
        self.assertEqual(beat["due"], (NOW + 71 * HOUR).isoformat())


class DisarmTest(unittest.TestCase):
    """Cancelling the dead man's switch, which a 200 from ntfy does not prove happened."""

    SEQ = "h-heartbeat"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "check.json"
        self.slept = []

    def tearDown(self):
        self.tmp.cleanup()

    def disarm(self, http, heartbeat=None, now=NOW):
        if heartbeat is not None:
            self.path.write_text(json.dumps({"heartbeat": heartbeat, "tracked": {}}))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = sw.run_disarm(make_watch({}, http, now=now), self.path, sleep=self.slept.append)
        self.stderr = err.getvalue()
        return code

    def test_retries_until_ntfy_really_drops_the_alert(self):
        """The first cancel is answered 200 and ignored, which is what disarm used to believe."""
        http = FakeHttp(armed=[self.SEQ], ignore_cancels=1)
        self.assertEqual(self.disarm(http), 0)
        self.assertEqual(http.cancelled, [self.SEQ, self.SEQ])
        self.assertEqual(http.armed, [])
        self.assertEqual(self.slept, [sw.CANCEL_SETTLE.total_seconds()])

    def test_one_cancel_is_enough_when_the_server_took_it(self):
        http = FakeHttp(armed=[self.SEQ])
        self.assertEqual(self.disarm(http), 0)
        self.assertEqual((http.cancelled, http.armed, self.slept), ([self.SEQ], [], []))

    def test_waits_out_the_second_in_which_a_cancel_would_be_ignored(self):
        """The installer cancels a heartbeat the check it just ran had scheduled."""
        http = FakeHttp(armed=[self.SEQ])
        self.assertEqual(self.disarm(http, {"due": (NOW + HOUR).isoformat(),
                                            "published": (NOW - 2 * SEC).isoformat()}), 0)
        self.assertEqual(self.slept, [sw.CANCEL_SETTLE.total_seconds() - 2])

    def test_does_not_wait_for_a_heartbeat_published_long_ago(self):
        http = FakeHttp(armed=[self.SEQ])
        self.assertEqual(self.disarm(http, {"due": (NOW + HOUR).isoformat(),
                                            "published": (NOW - HOUR).isoformat()}), 0)
        self.assertEqual(self.slept, [])

    def test_a_broken_published_timestamp_still_cancels(self):
        """Losing or corrupting state is a reason to disarm, not a reason not to. The wait is the
        only thing a bad timestamp can cost; what decides is the confirmation."""
        for bad in ("garbage", 17, None, [], "2026-13-45T99:99:99"):
            with self.subTest(published=bad):
                self.slept, http = [], FakeHttp(armed=[self.SEQ])
                self.assertEqual(self.disarm(http, {"due": (NOW + HOUR).isoformat(),
                                                    "published": bad}), 0)
                self.assertEqual((http.cancelled, http.armed), ([self.SEQ], []))

    def test_a_heartbeat_that_is_not_even_a_dict_still_cancels(self):
        http = FakeHttp(armed=[self.SEQ])
        self.path.write_text(json.dumps({"heartbeat": "wat", "tracked": {}}))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = sw.run_disarm(make_watch({}, http, now=NOW), self.path, sleep=self.slept.append)
        self.assertEqual((code, http.cancelled), (0, [self.SEQ]))

    def test_reports_an_alert_it_could_not_cancel(self):
        http = FakeHttp(armed=[self.SEQ], ignore_cancels=99)
        self.assertEqual(self.disarm(http, {"due": (NOW + HOUR).isoformat()}), 1)
        self.assertEqual(len(http.cancelled), sw.CANCEL_ATTEMPTS)
        self.assertIn("still armed", self.stderr)
        # The heartbeat is kept, so a later disarm knows there is still something to cancel.
        self.assertIn("heartbeat", json.loads(self.path.read_text()))

    def test_a_cancel_it_cannot_confirm_is_reported_but_not_failed(self):
        """A clean uninstall must not fail because ntfy wouldn't answer the follow-up question."""
        http = FakeHttp(armed=[self.SEQ], poll_error=urllib.error.URLError("refused"))
        self.assertEqual(self.disarm(http, {"due": (NOW + HOUR).isoformat()}), 0)
        self.assertEqual(http.cancelled, [self.SEQ])
        self.assertIn("could not confirm", self.stderr)
        self.assertEqual(json.loads(self.path.read_text()), {"tracked": {}})

    def test_a_cancel_that_cannot_be_sent_fails(self):
        self.assertEqual(self.disarm(raises(urllib.error.URLError("SENTINEL_SECRET"))), 1)
        self.assertIn("can't cancel", self.stderr)
        self.assertNotIn("SENTINEL", self.stderr)

    def test_an_unreadable_state_file_still_cancels(self):
        self.path.write_text("{not json")
        http = FakeHttp(armed=[self.SEQ])
        self.assertEqual(self.disarm(http), 0)
        self.assertEqual((http.cancelled, http.armed), ([self.SEQ], []))

    def test_pending_ignores_the_marker_a_successful_cancel_leaves(self):
        """The cancel's own tombstone shares the sequence ID, so matching on that alone would read
        every successful cancel as a failure and burn all three attempts every time."""
        http = FakeHttp(armed=[self.SEQ])
        notifier = sw.Notifier("https://ntfy.test", "topic", http)
        self.assertIs(notifier.pending(self.SEQ), True)
        notifier.delete(self.SEQ)
        self.assertIs(notifier.pending(self.SEQ), False)

    def test_pending_does_not_mistake_a_delivered_heartbeat_for_an_armed_one(self):
        """The bug the first live run found, on a cancel that had actually worked.

        `scheduled=1` adds the waiting messages to the topic's delivered history rather than
        returning them on their own, so every heartbeat this host has ever sent carries the same
        sequence ID as the one being cancelled. Matching the sequence in that one feed therefore
        reports "still armed" forever — every attempt spent, then a failure, about an alert that was
        cancelled on the first try. Only what `scheduled=1` adds is actually still waiting."""
        history = [self.SEQ] * 6  # six test heartbeats this host really did deliver
        http = FakeHttp(delivered=history)
        self.assertIs(sw.Notifier("https://ntfy.test", "topic", http).pending(self.SEQ), False)
        still_armed = FakeHttp(armed=[self.SEQ], delivered=history)
        self.assertIs(sw.Notifier("https://ntfy.test", "topic", still_armed).pending(self.SEQ), True)

    def test_a_cancel_that_worked_is_not_retried_because_of_old_deliveries(self):
        http = FakeHttp(armed=[self.SEQ], delivered=[self.SEQ] * 6)
        self.assertEqual(self.disarm(http, {"due": (NOW + HOUR).isoformat()}), 0)
        self.assertEqual((http.cancelled, self.slept), ([self.SEQ], []))

    def test_pending_answers_about_this_sequence_and_not_the_topic(self):
        """One topic carries every host's heartbeat and the --test one alongside the real one, so
        "is anything scheduled here?" is a different question from "is mine?". Asked the loose way,
        a disarm on one host spends every attempt and then reports failure because a different host
        is still checking in — and the alert it was actually asked to cancel is long gone."""
        http = FakeHttp(armed=["other-host-heartbeat", "h-test-heartbeat"])
        notifier = sw.Notifier("https://ntfy.test", "topic", http)
        self.assertIs(notifier.pending(self.SEQ), False)
        self.assertIs(notifier.pending("other-host-heartbeat"), True)

    def test_pending_says_it_does_not_know_rather_than_guessing(self):
        for failure in (urllib.error.URLError("refused"), sw.HTTPException("truncated")):
            with self.subTest(failure=type(failure).__name__):
                http = FakeHttp(armed=[self.SEQ], poll_error=failure)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertIsNone(sw.Notifier("https://ntfy.test", "topic", http).pending(self.SEQ))

    def test_dry_run_neither_polls_nor_waits(self):
        http = FakeHttp(armed=[self.SEQ])
        self.path.write_text(json.dumps({"heartbeat": {"published": NOW.isoformat()}}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sw.run_disarm(
                sw.Watch({}, sw.Notifier("https://ntfy.test", "topic", http, dry_run=True), NOW, http,
                         None, "h", "h"), self.path, dry_run=True, sleep=self.slept.append), 0)
        self.assertEqual((http.calls, self.slept), ([], []))


class RunCheckTest(unittest.TestCase):
    def check(self, state, run, at, cfg=None, http=None, timing=sw.REAL_TIMING):
        http = FakeHttp({"/health": "Healthy"}) if http is None else http
        # DISK_FREE_MIN off unless a test asks for it: SABNZBD_TEMP now falls back to Compose's
        # /tmp/sabnzbd-temp, so a check with no disk settings reads this machine's real /tmp.
        cfg = {"STACK_DIR": "/stack", "JELLYFIN_URL": "http://jf", "HEARTBEAT_GRACE": "off",
               "DISK_FREE_MIN": "off"} | (cfg or {})
        sw.run_check(make_watch(cfg, http, run, NOW + at, timing), state)
        return http.published()

    def test_docker_down_alerts_on_the_second_run_without_leaking_stderr(self):
        state = {}
        self.assertEqual(self.check(state, docker_down, 0 * MIN), [])
        sent = self.check(state, docker_down, 15 * MIN)
        self.assertEqual([(m["title"], m["message"]) for m in sent],
                         [("Media stack on h: 1 problem", "• docker: not responding (details in the journal)")])
        self.assertNotIn("SENTINEL", json.dumps(sent))

    def test_services_are_not_called_recovered_while_docker_is_down(self):
        exited = docker(container("sonarr", "exited", exit_code=1), container("radarr"))
        state, sent = {}, []
        for minutes, run in ((0, exited), (15, exited), (30, docker_down), (45, docker_down), (60, docker_down),
                             (75, exited), (90, exited), (105, exited)):
            sent += [(minutes, m["title"], m["message"]) for m in self.check(state, run, minutes * MIN)]
        self.assertEqual(sent, [
            (15, "Media stack on h: 1 problem", "• sonarr: exited (code 1)"),
            (45, "Media stack on h: 2 problems",
             "• docker: not responding (details in the journal)\n• sonarr: exited (code 1)"),
            # Docker coming back is named, not silently dropped from the list: the artifex is being
            # told the stack is down to one problem, and which one stopped is the half of that he
            # can act on.
            (105, "Media stack on h: 1 problem", "• sonarr: exited (code 1)\n\nCleared:\n"
             "• docker: not responding (details in the journal)"),
        ])

    def test_services_are_kept_while_the_compose_file_is_unreadable(self):
        exited = docker(container("sonarr", "exited", exit_code=1), container("radarr"))
        state = {}
        for minutes in (0, 15):
            self.check(state, exited, minutes * MIN)
        with contextlib.redirect_stderr(io.StringIO()):
            self.check(state, raises(RuntimeError("bad compose file")), 30 * MIN)
        self.assertTrue(state["tracked"]["service:sonarr"]["alerted"])
        self.assertEqual(state["tracked"]["service:sonarr"]["absent"], 0)

    UNPATCHED = "seerr: running without its download-tracker patch (see images/seerr/README.md)"
    CANT_CHECK = "seerr: can't check its download-tracker patch (details in the journal)"

    def test_an_unpatched_seerr_alerts_like_any_other_problem(self):
        unpatched = seerr_stack(container("sonarr"), container("seerr"), tracker=UNPATCHED_TRACKER)
        state = {}
        self.assertEqual(self.check(state, unpatched, 0 * MIN), [])
        [sent] = self.check(state, unpatched, 15 * MIN)
        self.assertEqual((sent["title"], sent["message"]), ("Media stack on h: 1 problem", f"• {self.UNPATCHED}"))
        patched = seerr_stack(container("sonarr"), container("seerr"))
        self.assertEqual(self.check(state, patched, 30 * MIN), [])  # absence is damped like any other
        self.assertEqual(self.check(state, patched, 45 * MIN), [])  # and held to the stack gap
        self.assertNotIn("seerr:patch", state["tracked"])
        [sent] = self.check(state, patched, 75 * MIN)
        self.assertEqual(sent["message"], f"Everything that was down is back up.\n\nCleared:\n• {self.UNPATCHED}")

    def alerted_unpatched(self):
        unpatched = seerr_stack(container("sonarr"), container("seerr"), tracker=UNPATCHED_TRACKER)
        state = {}
        for minutes in (0, 15):
            self.check(state, unpatched, minutes * MIN)
        self.assertTrue(state["tracked"]["seerr:patch"]["alerted"])
        return state

    def test_a_failed_read_does_not_reword_a_standing_unpatched_alert(self):
        """A read that fails says nothing new about a Seerr already seen unpatched. Rewording the
        alert to "can't check" and back counted as two worsenings: two extra High notifications that
        skip the hour's spacing and spend two of the day's twelve."""
        state = self.alerted_unpatched()
        unpatched = seerr_stack(container("sonarr"), container("seerr"), tracker=UNPATCHED_TRACKER)
        sent = []
        with contextlib.redirect_stderr(io.StringIO()):
            for i, failure in enumerate((subprocess.TimeoutExpired(["docker"], 5), "", RuntimeError("gone"))):
                unreadable = seerr_stack(container("sonarr"), container("seerr"), tracker=failure)
                start = 30 + 75 * i
                for minutes in (start, start + 15, start + 30):
                    sent += self.check(state, unreadable, minutes * MIN)
                for minutes in (start + 45, start + 60):
                    sent += self.check(state, unpatched, minutes * MIN)
                self.assertEqual(state["tracked"]["seerr:patch"]["description"], self.UNPATCHED)
        self.assertEqual(sent, [])
        self.assertTrue(state["tracked"]["seerr:patch"]["alerted"])

    def test_the_first_alert_after_a_single_unpatched_sighting_says_the_read_failed(self):
        """One sighting is not a standing alert. The wording is pinned to stop a notification that
        has already gone out from being reworded; before the first one there is nothing to protect,
        and "confirmed once, then couldn't re-check" is a different, more urgent story than "running
        unpatched" — docker exec into Seerr may be broken, or Seerr crash-looping. Saying the certain
        thing would send the artifex after the wrong fault on the one message he actually gets."""
        unpatched = seerr_stack(container("sonarr"), container("seerr"), tracker=UNPATCHED_TRACKER)
        unreadable = seerr_stack(container("sonarr"), container("seerr"), tracker=RuntimeError("gone"))
        state = {}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.check(state, unpatched, 0 * MIN), [])
            [sent] = self.check(state, unreadable, 15 * MIN)
        self.assertEqual(sent["message"], f"• {self.CANT_CHECK}")

    def test_the_wording_is_pinned_from_the_run_after_the_alert_went_out(self):
        """The boundary itself. CONSECUTIVE_RUNS is 2, so the run reaching a count of 2 is the run
        that publishes: a count test and an `alerted` test differ by exactly one run, and it is the
        run whose wording the artifex reads first."""
        unpatched = seerr_stack(container("sonarr"), container("seerr"), tracker=UNPATCHED_TRACKER)
        unreadable = seerr_stack(container("sonarr"), container("seerr"), tracker=RuntimeError("gone"))
        state, sent = {}, []
        with contextlib.redirect_stderr(io.StringIO()):
            for minutes, run in ((0, unpatched), (15, unpatched), (30, unreadable)):
                sent += self.check(state, run, minutes * MIN)
        # Published at 15 on the second sighting; the failed read at 30 is held to that wording, so
        # it is not sent again as a worsening.
        self.assertEqual([m["message"] for m in sent], [f"• {self.UNPATCHED}"])
        self.assertEqual(state["tracked"]["seerr:patch"]["description"], self.UNPATCHED)

    def test_a_standing_cannot_check_alert_is_never_promoted_to_running_unpatched(self):
        """The wording is pinned to the sentence that went out, not to the unpatched one. A Seerr
        that has only ever been unreadable is alerted as unreadable, and every failed read after it
        says the same thing — inventing a diagnosis nobody read off the container would send the
        artifex rebuilding an image when the fault is that he can't get into it."""
        unreadable = seerr_stack(container("sonarr"), container("seerr"), tracker=RuntimeError("gone"))
        state, sent = {}, []
        with contextlib.redirect_stderr(io.StringIO()):
            for minutes in (0, 15, 30, 45):
                sent += self.check(state, unreadable, minutes * MIN)
        self.assertEqual([m["message"] for m in sent], [f"• {self.CANT_CHECK}"])
        self.assertEqual(state["tracked"]["seerr:patch"]["description"], self.CANT_CHECK)

    def test_an_alert_ntfy_refused_does_not_pin_the_wording_of_the_next_one(self):
        """`alerted` is set before publish_stack runs and stays set if that publish throws, so it is
        not the same fact as "the artifex has seen this". With ntfy refusing, the unpatched sighting
        is marked alerted while nothing has gone out; the failed read after it is then still the
        first message, and has to say so."""
        unpatched = seerr_stack(container("sonarr"), container("seerr"), tracker=UNPATCHED_TRACKER)
        unreadable = seerr_stack(container("sonarr"), container("seerr"), tracker=RuntimeError("gone"))
        state = {}
        with contextlib.redirect_stderr(io.StringIO()):
            for minutes in (0, 15):
                refused = FakeHttp({"/health": "Healthy"}, fail_posts_after=0)
                with contextlib.suppress(Exception):
                    self.check(state, unpatched, minutes * MIN, http=refused)
            self.assertTrue(state["tracked"]["seerr:patch"]["alerted"])
            self.assertEqual(state["stack"]["shown"], {})
            [sent] = self.check(state, unreadable, 30 * MIN)
        self.assertEqual(sent["message"], f"• {self.CANT_CHECK}")

    def test_an_alert_inherited_from_before_the_stack_record_still_pins_the_wording(self):
        """The upgrade path: a state file that alerted the patch before `stack` existed. `shown` is
        seeded from those entries, so the wording is held — reading the bare record instead would
        reword a standing alert on the first run after an upgrade, which is the whole defect."""
        unreadable = seerr_stack(container("sonarr"), container("seerr"), tracker=RuntimeError("gone"))
        state = {"tracked": {"seerr:patch": {
            "count": 4, "first": (NOW - 60 * MIN).isoformat(), "seen": (NOW - 15 * MIN).isoformat(),
            "alerted": True, "description": self.UNPATCHED, "absent": 0}}}
        with contextlib.redirect_stderr(io.StringIO()):
            sent = self.check(state, unreadable, 0 * MIN)
        self.assertEqual(sent, [])
        self.assertEqual(state["tracked"]["seerr:patch"]["description"], self.UNPATCHED)

    def test_a_patch_entry_carried_forward_is_not_the_one_the_old_state_holds(self):
        """Docker being down, and a stopped Seerr, both carry the last patch entry forward. Holding
        the same dict `previous` holds means a later edit to it rewrites what this run has already
        read — `inherited` reads descriptions straight back out of `previous`. The guard only ever
        writes a value the entry already has, so nothing is wrong today; this keeps it that way."""
        stopped = seerr_stack(container("sonarr"), container("seerr", "exited", exit_code=137),
                              tracker=RuntimeError("container is not running"))
        for run in (docker_down, stopped):
            state = self.alerted_unpatched()
            before = state["tracked"]["seerr:patch"]
            with contextlib.redirect_stderr(io.StringIO()):
                self.check(state, run, 30 * MIN)
            self.assertEqual(state["tracked"]["seerr:patch"]["description"], self.UNPATCHED)
            self.assertIsNot(state["tracked"]["seerr:patch"], before)

    def test_an_unreadable_seerr_not_seen_unpatched_lately_says_it_cannot_check(self):
        """A failed read is what it is until an unpatched alert has actually been published. Below,
        first because the gap makes the sighting stale so nothing is ever alerted, then because the
        patch was found and the alert cleared.

        Not because of the gap itself: `advance` short-circuits the staleness check on `alerted`, so
        once the alert is out, silence of any length keeps the pin. That is deliberate — an alert
        nobody has cleared is still standing, however long ago it went out."""
        unpatched = seerr_stack(container("sonarr"), container("seerr"), tracker=UNPATCHED_TRACKER)
        patched = seerr_stack(container("sonarr"), container("seerr"))
        unreadable = seerr_stack(container("sonarr"), container("seerr"), tracker=RuntimeError("gone"))
        cant = f"• {self.CANT_CHECK}"
        with contextlib.redirect_stderr(io.StringIO()):
            state = {}
            self.check(state, unpatched, 0 * MIN)
            self.check(state, unreadable, 60 * MIN)  # stale: the sighting at 0 no longer counts
            [sent] = self.check(state, unreadable, 75 * MIN)
            self.assertEqual(sent["message"], cant)

            state = self.alerted_unpatched()
            sent = []
            for minutes, run in ((30, patched), (45, patched), (75, patched), (90, unreadable), (105, unreadable)):
                sent += self.check(state, run, minutes * MIN)
            self.assertEqual([m["message"] for m in sent],
                             [f"Everything that was down is back up.\n\nCleared:\n• {self.UNPATCHED}", cant])

    def test_the_patch_is_not_called_fixed_while_docker_cannot_be_asked(self):
        """Docker being down says nothing about what Seerr is running. Reading its silence as
        recovery would announce "Cleared" for a patch nobody checked, then re-alert when Docker
        returned."""
        state = self.alerted_unpatched()
        sent = []
        with contextlib.redirect_stderr(io.StringIO()):
            for minutes in (30, 45, 60):
                sent += self.check(state, docker_down, minutes * MIN)
        self.assertEqual([m["message"] for m in sent],
                         [f"• docker: not responding (details in the journal)\n• {self.UNPATCHED}"])
        self.assertTrue(state["tracked"]["seerr:patch"]["alerted"])

    def test_the_patch_is_not_called_fixed_while_seerr_is_stopped(self):
        state = self.alerted_unpatched()
        stopped = seerr_stack(container("sonarr"), container("seerr", "exited", exit_code=137),
                              tracker=RuntimeError("container is not running"))
        sent = []
        for minutes in (30, 45, 60):
            sent += self.check(state, stopped, minutes * MIN)
        self.assertEqual([m["message"] for m in sent], [f"• seerr: exited (code 137)\n• {self.UNPATCHED}"])
        self.assertEqual(execs(stopped), [])

    def test_a_stopped_seerr_with_no_patch_history_is_just_a_stopped_service(self):
        stopped = seerr_stack(container("sonarr"), container("seerr", "exited", exit_code=137))
        state = {}
        for minutes in (0, 15):
            sent = self.check(state, stopped, minutes * MIN)
        self.assertEqual([m["message"] for m in sent], ["• seerr: exited (code 137)"])
        self.assertNotIn("seerr:patch", state["tracked"])

    def test_unexpected_error_gathering_problems_still_reschedules_the_heartbeat(self):
        state, http = {}, FakeHttp()
        with self.assertRaises(KeyError):
            self.check(state, raises(KeyError("name")), 0 * MIN, {"HEARTBEAT_GRACE": "24h"}, http)
        self.assertEqual([m["title"] for m in http.published()], ["h is unreachable"])
        self.assertNotIn("tracked", state)

    def test_state_from_before_the_stack_record_does_not_repeat_an_alert(self):
        state = {"tracked": {"service:sonarr": {"count": 5, "alerted": True, "description": "sonarr: exited (code 1)",
                                                "seen": NOW.isoformat()}}}
        exited = docker(container("sonarr", "exited", exit_code=1), container("radarr"))
        self.assertEqual(self.check(state, exited, 15 * MIN), [])

    def test_upgrading_from_a_role_set_disk_key_says_nothing_at_all(self):
        """The key used to be disk:library+downloads. On the first run after the upgrade that key
        retires and two unseen ones arrive, which publish_stack can only read as new problems: a
        rotating_light, at top priority, about a drive where nothing has happened. And since the
        floor went with the retired key, a drive that had recovered from 5G to 40G within the episode
        would be recomputed at a higher step, so the emergency would carry *better* news than the
        message it replaced. Nothing changed, so nothing should be sent."""
        old = "disk: dropped below 10G free for the library and downloads"
        state = {"disk": {"disk:library+downloads": 10 * GB},
                 "tracked": {"disk:library+downloads": {"count": 40, "alerted": True, "absent": 0,
                                                        "description": old,
                                                        "first": (NOW - 4 * DAY).isoformat(),
                                                        "seen": NOW.isoformat()}},
                 "stack": {"shown": {"disk:library+downloads": old},
                           "sent": [(NOW - 2 * HOUR).isoformat()]}}
        cfg = {"MEDIA_ROOT": "/lib", "SABNZBD_TEMP": "/lib", "DISK_FREE_MIN": "100G"}
        healthy = docker(container("sonarr"), container("radarr"))
        with mounted(Drive(40 * GB)):  # recovered within the episode; the ratchet must hold at 10G
            self.assertEqual(self.check(state, healthy, 15 * MIN, cfg), [])
        self.assertEqual(state["stack"]["shown"], {"disk:library": old, "disk:downloads": old})
        self.assertEqual(state["disk"], {"disk:library": {"device": 1, "level": 10 * GB},
                                         "disk:downloads": {"device": 1, "level": 10 * GB}})

    def test_a_stack_record_written_before_sent_existed_does_not_repeat_an_alert(self):
        """The previous upgrade path wrote {"shown": ...} with no "sent", so on disk right now there
        are records that look exactly like "nothing has gone out in over a day" — which is the very
        condition STACK_REPEAT fires on."""
        state = {"stack": {"shown": {"service:sonarr": "sonarr: exited (code 1)"}},
                 "tracked": {"service:sonarr": {"count": 5, "alerted": True, "absent": 0,
                                                "description": "sonarr: exited (code 1)",
                                                "seen": NOW.isoformat()}}}
        exited = docker(container("sonarr", "exited", exit_code=1), container("radarr"))
        self.assertEqual(self.check(state, exited, 15 * MIN), [])
        self.assertEqual(state["stack"]["sent"], [(NOW + 15 * MIN).isoformat()])

    def test_a_first_install_starts_with_a_clean_send_history(self):
        """The stack record is seeded as already sent so STACK_REPEAT doesn't re-send every inherited
        alert on the first run after an upgrade. With nothing inherited there is nothing to seed, and
        dating a send that never happened would spend a slot of STACK_DAILY_CAP on it."""
        state = {}
        self.assertEqual(self.check(state, docker(container("sonarr"), container("radarr")), 0 * MIN), [])
        self.assertEqual(state["stack"], {"shown": {}, "sent": []})

    def test_stack_problems_reads_the_compose_projects_containers(self):
        outputs = {"config": COMPOSE_CONFIG, "ps": "abc\ndef\n",
                   "inspect": json.dumps([container("sonarr"), container("radarr", "exited", exit_code=1)])}
        seen = []

        def run(argv):
            seen.append(argv)
            return outputs[argv[1] if argv[1] != "compose" else "config"]
        self.assertEqual(sw.stack_problems(Path("/stack"), run), {"service:radarr": "radarr: exited (code 1)"})
        self.assertIn("label=com.docker.compose.project=proj", seen[1])
        self.assertEqual(seen[2], ["docker", "inspect", "abc", "def"])

    def test_unreadable_compose_config_is_labelled_without_leaking_stderr(self):
        """Compose quotes .env values in its errors; those must never reach the public topic."""
        with contextlib.redirect_stderr(io.StringIO()):
            problems = sw.stack_problems(Path("/stack"), raises(RuntimeError("line 3: 'SENTINEL_SECRET")))
        self.assertEqual(problems, {"compose": "compose: can't read the stack config (details in the journal)"})

    def test_disk_is_watched_alongside_the_containers(self):
        """The path is a sentinel, so the leak assertion can actually fail: the topic is public and
        the real paths name the user's home directory."""
        state = {}
        cfg = {"MEDIA_ROOT": "/SENTINEL_HOME/media", "DISK_FREE_MIN": "100G"}
        healthy = docker(container("sonarr"), container("radarr"))
        with mounted(Drive(30 * GB, at="/SENTINEL_HOME/media")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.check(state, healthy, 0 * MIN, cfg), [])
            [sent] = self.check(state, healthy, 15 * MIN, cfg)
        self.assertEqual(sent["title"], "Media stack on h: 1 problem")
        self.assertEqual(sent["message"],
                         "• disk: dropped below 50G free for the library (now 30G free)")
        self.assertNotIn("SENTINEL", json.dumps(sent))

    def test_a_worsening_step_is_not_dressed_as_good_news(self):
        """The ratchet made a changed description the disk's escalation path, and publish_stack only
        counted new keys as worse: "dropped below 25G" went out at Default with a white_check_mark."""
        state, cfg = {}, {"MEDIA_ROOT": "/lib", "DISK_FREE_MIN": "100G"}
        healthy = docker(container("sonarr"), container("radarr"))
        drive, sent = Drive(60 * GB), []
        with mounted(drive):
            for minutes in (0, 15):
                sent += self.check(state, healthy, minutes * MIN, cfg)
            drive.free = 20 * GB
            for minutes in (30, 45):
                sent += self.check(state, healthy, minutes * MIN, cfg)
        self.assertEqual([(m["message"], m["priority"], m["tags"]) for m in sent], [
            ("• disk: dropped below 100G free for the library (now 60G free)",
             sw.HIGH, ["rotating_light"]),
            ("• disk: dropped below 25G free for the library (now 20G free)",
             sw.HIGH, ["rotating_light"])])

    def test_a_drive_wobbling_across_a_step_does_not_burn_the_daily_cap(self):
        """SABnzbd pauses at its own floor and resumes when space returns, so free space oscillates
        around exactly the kind of number a step sits on. Undamped that sent 10 alternating updates
        in 11.5 hours and left almost nothing of STACK_DAILY_CAP for the fall that followed."""
        state, cfg = {}, {"MEDIA_ROOT": "/lib", "DISK_FREE_MIN": "100G"}
        healthy = docker(container("sonarr"), container("radarr"))
        drive, sent = Drive(52 * GB), []
        with mounted(drive):
            for minutes in range(0, 12 * 60, 15):
                drive.free = (52 if (minutes // 15) % 2 else 48) * GB
                sent += self.check(state, healthy, minutes * MIN, cfg)
            # 48 checks over 12 hours; undamped this alternated 10 times. The one that goes out is
            # sent on an up-swing, and says so: the ratchet's 50G is where the drive has been, "now
            # 52G free" is where it is, and only the first of those is allowed to move the alert.
            self.assertEqual([m["message"] for m in sent],
                             ["• disk: dropped below 50G free for the library (now 52G free)"])
            # ...and the fall that follows is still heard, because the cap was not spent on noise.
            for index, free in enumerate(range(48, -1, -3)):
                drive.free = free * GB
                sent += self.check(state, healthy, (12 * 60 + index * 15) * MIN, cfg)
        self.assertEqual([m["message"] for m in sent],
                         ["• disk: dropped below 50G free for the library (now 52G free)",
                          "• disk: dropped below 25G free for the library (now 24G free)",
                          "• disk: dropped below 10G free for the library (now 9G free)"])

    def test_the_floor_is_released_once_the_drive_recovers(self):
        """Otherwise the next time it dipped it would still claim the old low point."""
        state, cfg = {}, {"MEDIA_ROOT": "/lib", "DISK_FREE_MIN": "100G"}
        healthy = docker(container("sonarr"), container("radarr"))
        drive = Drive(20 * GB)
        with mounted(drive):
            for minutes in (0, 15):
                self.check(state, healthy, minutes * MIN, cfg)
            self.assertEqual(state["disk"], {"disk:library": {"device": 1, "level": 25 * GB}})
            drive.free = 500 * GB
            for minutes in (30, 45, 60):
                self.check(state, healthy, minutes * MIN, cfg)
            self.assertEqual(state["disk"], {})
            drive.free = 90 * GB
            for minutes in (75, 90):
                sent = self.check(state, healthy, minutes * MIN, cfg)
        self.assertEqual([m["message"] for m in sent],
                         ["• disk: dropped below 100G free for the library (now 90G free)"])

    def test_a_drive_parked_below_the_threshold_is_heard_again_a_day_later(self):
        """A settled problem used to be announced exactly once, ever — publish_stack returns early on
        current == shown and nothing here had FAILURE_REPEAT's re-nudge. The ratchet made the text
        settle sooner, so the disk inherited the worst of it: one notification, possibly swiped away
        weeks ago, and no all clear until somebody frees space."""
        state, cfg = {}, {"MEDIA_ROOT": "/lib", "DISK_FREE_MIN": "100G"}
        healthy, sent = docker(container("sonarr"), container("radarr")), []
        with mounted(Drive(5 * GB)):
            for minutes in (0, 15):
                sent += [(minutes * MIN, m) for m in self.check(state, healthy, minutes * MIN, cfg)]
            for hours in range(1, 49):  # two days of quarter-hourly checks, sampled hourly
                sent += [(hours * HOUR, m) for m in self.check(state, healthy, hours * HOUR, cfg)]
        self.assertEqual([(at, m["tags"]) for at, m in sent],
                         [(15 * MIN, ["rotating_light"]), (25 * HOUR, ["warning"])])
        self.assertEqual(sent[1][1]["message"],
                         "• disk: dropped below 10G free for the library (now 5G free)\n\n"
                         f"Still not resolved. First seen {sw.when(NOW)}.")

    def test_moving_downloads_to_its_own_drive_is_not_a_new_problem(self):
        """[7] at the level that matters. The key used to carry the role set, so the split retired
        disk:library+downloads and introduced two unknown keys: a "new problem" for a drive that had
        been low for days, and both reports back at the top step because the floor went with the old
        key. Keyed per role, both keys and both floors survive the split. The update still goes out
        at High, because both descriptions change and publish_stack reads any changed description as
        a worsening — one deliberate alert during a migration the artifex is performing by hand,
        which is a different thing from inventing a problem."""
        state = {"disk": {}}
        cfg = {"MEDIA_ROOT": "/lib", "SABNZBD_TEMP": "/dl", "DISK_FREE_MIN": "100G"}
        healthy, sent = docker(container("sonarr"), container("radarr")), []
        layout = {"/lib": (1, 20 * GB, 4096 * GB), "/dl": (1, 20 * GB, 4096 * GB)}
        with mounted(lambda path: layout[path]):
            for minutes in (0, 15):
                sent += self.check(state, healthy, minutes * MIN, cfg)
            self.assertEqual(state["disk"], {"disk:library": {"device": 1, "level": 25 * GB},
                                             "disk:downloads": {"device": 1, "level": 25 * GB}})
            layout["/dl"] = (2, 20 * GB, 4096 * GB)  # same free space, now its own filesystem
            for minutes in (30, 45):
                sent += self.check(state, healthy, minutes * MIN, cfg)
        self.assertEqual([(m["title"], m["message"]) for m in sent], [
            ("Media stack on h: 1 problem",
             "• disk: dropped below 25G free for the library and downloads (now 20G free)"),
            ("Media stack on h: 2 problems",
             "• disk: dropped below 25G free for the downloads (now 20G free)\n"
             "• disk: dropped below 25G free for the library (now 20G free)")])
        self.assertEqual(state["disk"], {"disk:library": {"device": 1, "level": 25 * GB},
                                         "disk:downloads": {"device": 2, "level": 25 * GB}})
        self.assertEqual(set(state["tracked"]), {"disk:library", "disk:downloads"})

    def test_a_fallback_that_vanishes_never_becomes_a_false_all_clear(self):
        """End to end, which is where this one actually bites: the skip is in disk_problems, the
        damage is two runs later in publish_stack. Compose's tmpfs going away used to read as the
        downloads drive recovering — "Everything that was down is back up. Cleared: disk: dropped
        below 10G free for the downloads" — when nothing had been freed and the path had simply
        stopped answering."""
        state, cfg = {}, {"SABNZBD_TEMP": "", "DISK_FREE_MIN": "100G"}
        healthy, sent = docker(container("sonarr"), container("radarr")), []
        present = {"/tmp/sabnzbd-temp": (2, 5 * GB, 4096 * GB)}

        def reader(path, timeout=None):
            if path not in present:
                raise OSError(2, "No such file or directory")
            return present[path]
        with mounted(reader), contextlib.redirect_stderr(io.StringIO()):
            for minutes in (0, 15):
                sent += self.check(state, healthy, minutes * MIN, cfg)
            present.clear()  # the tmpfs is gone; its free space is now unknown, not recovered
            for hours in range(1, 6):
                sent += self.check(state, healthy, hours * HOUR, cfg)
        self.assertEqual([(m["title"], m["message"]) for m in sent], [
            ("Media stack on h: 1 problem",
             "• disk: dropped below 10G free for the downloads (now 5G free)"),
            ("Media stack on h: 1 problem",
             "• disk: can't read free space for the downloads path")])
        self.assertTrue(state["tracked"]["disk:downloads"]["alerted"])

    def test_moving_a_role_to_a_healthier_but_still_low_drive_goes_out_at_high(self):
        """A documented tradeoff, not a defect, and tested so it stays a decision. MEDIA_ROOT moving
        from a drive at 5G to one at 60G is genuinely news — it is a different filesystem, and the
        floor deliberately does not follow the role onto it — but publish_stack reads any changed
        description as a worsening, so better news arrives at High under a rotating_light, and its
        sentence names a larger number than the one it replaces.

        Left as it is on purpose. Ranking the two would mean comparing a level measured on one drive
        against a level measured on another, which is the falsehood 2f3529a removed from the report
        itself; re-introducing it to pick a priority buys a quieter tone with the same lie. The drive
        really is under the threshold, and the bullet says what it actually has."""
        state, cfg = {}, {"MEDIA_ROOT": "/lib", "DISK_FREE_MIN": "100G"}
        healthy, sent = docker(container("sonarr"), container("radarr")), []
        drive = {"device": 1, "free": 5 * GB}

        def reader(path, timeout=None):
            if path != "/lib":
                raise OSError(2, "No such file or directory")
            return drive["device"], drive["free"], 4096 * GB
        with mounted(reader), contextlib.redirect_stderr(io.StringIO()):
            for minutes in (0, 15):
                sent += self.check(state, healthy, minutes * MIN, cfg)
            drive.update(device=9, free=60 * GB)  # same role, a different and healthier drive
            for minutes in (30, 45):
                sent += self.check(state, healthy, minutes * MIN, cfg)
        self.assertEqual([(m["message"], m["priority"], m["tags"]) for m in sent], [
            ("• disk: dropped below 10G free for the library (now 5G free)",
             sw.HIGH, ["rotating_light"]),
            ("• disk: dropped below 100G free for the library (now 60G free)",
             sw.HIGH, ["rotating_light"])])
        self.assertEqual(state["disk"], {"disk:library": {"device": 9, "level": 100 * GB}})

    def test_a_days_later_repeat_does_not_read_as_a_claim_about_the_present(self):
        """The ratchet is what lets a sentence settle, and a settled sentence is what the daily
        re-send re-sends. A day after the low point, "dropped below 10G" goes out word for word to an
        artifex who has since freed 65G — "still not resolved" is true of the problem, and was the
        first half of this fix, but the sentence above it still described the worst moment of
        yesterday as though it were now."""
        state, cfg = {}, {"MEDIA_ROOT": "/lib", "DISK_FREE_MIN": "100G"}
        healthy, sent = docker(container("sonarr"), container("radarr")), []
        drive = Drive(5 * GB)
        with mounted(drive):
            for minutes in (0, 15):
                sent += self.check(state, healthy, minutes * MIN, cfg)
            drive.free = 70 * GB  # 65G freed: still under DISK_FREE_MIN, so still a problem
            for hours in range(1, 26):
                sent += self.check(state, healthy, hours * HOUR, cfg)
        self.assertEqual([m["message"] for m in sent], [
            "• disk: dropped below 10G free for the library (now 5G free)",
            "• disk: dropped below 10G free for the library (now 70G free)\n\n"
            f"Still not resolved. First seen {sw.when(NOW)}."])

    def test_disk_check_is_off_by_the_setting(self):
        state, cfg = {}, {"MEDIA_ROOT": "/", "DISK_FREE_MIN": "off"}
        healthy = docker(container("sonarr"), container("radarr"))
        for minutes in (0, 15):
            self.assertEqual(self.check(state, healthy, minutes * MIN, cfg), [])

    def test_a_rejected_disk_setting_becomes_a_failing_alert_not_a_silent_skip(self):
        with self.assertRaises(sw.WatchError):
            self.check({}, docker(), 0 * MIN, {"MEDIA_ROOT": "/", "DISK_FREE_MIN": "lots"})

    def test_blank_jellyfin_url_means_default(self):
        http = FakeHttp({"/health": "Healthy"})
        self.check({}, docker(), 0 * MIN, {"JELLYFIN_URL": ""}, http)
        self.assertIn("http://localhost:8096/health", [url for _, url, _, _ in http.calls])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = {"STACK_DIR": self.tmp.name, "STATE_DIR": self.tmp.name, "NTFY_TOPIC": "t",
                    "NTFY_SERVER": "https://ntfy.test", "WATCH_HOSTNAME": "zen box",
                    "CONFIG_ROOT": self.tmp.name}
        self.quiet = dict(self.env, HEARTBEAT_GRACE="off", JELLYFIN_URL="off")
        self.slept = []
        for name in ("radarr", "sonarr"):
            conf = self.dir / "config" / name
            conf.mkdir(parents=True)
            conf.joinpath("config.xml").write_text("<Port>1</Port><ApiKey>k</ApiKey>")

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *argv, env=None, http=None, run=None, now=NOW):
        # Never the real sleep: disarm waits out ntfy's blind spot and retries, so a regression
        # anywhere in that path would otherwise be paid for in wall-clock seconds by every run of
        # the suite — which is how a slow suite teaches people to stop running it.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sw.main(list(argv), self.env if env is None else env,
                           FakeHttp() if http is None else http, run, now, sleep=self.slept.append)

    def saved(self, name):
        return json.loads((self.dir / name).read_text())

    def test_missing_topic(self):
        self.assertEqual(self.main("check", env=dict(self.env, NTFY_TOPIC="")), 2)

    def test_failure_alert_names_the_cause_and_repeats_daily(self):
        broken = FakeHttp({"/api/v3/": urllib.error.HTTPError("http://r?SENTINEL_SECRET", 401, "SENTINEL_SECRET", {},
                                                                 None)})
        times = (NOW, NOW + HOUR, NOW + 2 * HOUR, NOW + 25 * HOUR - MIN, NOW + 25 * HOUR)
        self.assertEqual([self.main("stalled", http=broken, now=at) for at in times], [1] * 5)
        failing = broken.published()
        self.assertEqual([(m["title"], m["sequence_id"], m["priority"]) for m in failing],
                         [("Stack watch on zen box is failing", "zen-box-watch-stalled", 4)] * 2)
        self.assertEqual(failing[0]["message"],
                         f"'stalled' has failed 2 runs in a row, since {sw.when(NOW)}: radarr: HTTP 401. "
                         "Its alerts are off until this is fixed. Details: journalctl --user -u 'media-stack-*'")
        self.assertIn("failed 5 runs in a row", failing[1]["message"])
        self.assertNotIn("SENTINEL", json.dumps(failing))

        fixed = FakeHttp({"/api/v3/": paged([])})
        self.assertEqual(self.main("stalled", http=fixed, now=NOW + 26 * HOUR), 0)
        self.assertEqual([m["title"] for m in fixed.published()], ["Stack watch on zen box is working again"])
        self.assertEqual(self.saved("stalled.json"), {"notified": {}})

    def test_unexpected_exception_text_never_reaches_a_notification(self):
        http = FakeHttp()
        for minutes in (0, 15):
            self.main("check", env=self.quiet, http=http, run=raises(ValueError("SENTINEL_SECRET")),
                      now=NOW + minutes * MIN)
        self.assertEqual(len(http.published()), 1)
        self.assertIn(": ValueError. Its alerts are off", http.published()[0]["message"])
        self.assertNotIn("SENTINEL", json.dumps(http.published()))

    def test_single_failure_then_success_is_silent(self):
        broken = FakeHttp({"/api/v3/": urllib.error.URLError("refused")})
        self.assertEqual(self.main("stalled", http=broken), 1)
        fixed = FakeHttp({"/api/v3/": paged([])})
        self.assertEqual(self.main("stalled", http=fixed), 0)
        self.assertEqual(broken.published() + fixed.published(), [])
        self.assertEqual(self.saved("stalled.json"), {"notified": {}})

    def test_undelivered_failure_alert_is_retried(self):
        for _ in range(3):
            self.main("stalled", http=raises(urllib.error.URLError("offline")))
        self.assertEqual(self.saved("stalled.json")["failure"], {"count": 3, "since": NOW.isoformat()})
        recovering = FakeHttp({"/api/v3/": urllib.error.URLError("still refused")})
        self.main("stalled", http=recovering)
        self.assertEqual(len(recovering.published()), 1)

    def test_upgrades_the_old_failure_counter(self):
        (self.dir / "stalled.json").write_text(json.dumps({"failures": 1, "notified": {}}))
        broken = FakeHttp({"/api/v3/": urllib.error.URLError("refused")})
        self.main("stalled", http=broken)
        self.assertEqual([m["title"] for m in broken.published()], ["Stack watch on zen box is failing"])
        self.assertNotIn("failures", self.saved("stalled.json"))

    def test_corrupt_state_is_moved_aside_and_reported(self):
        for content in ("", "null", "[]", '{"tracked": {'):
            with self.subTest(content=content):
                (self.dir / "check-test.json").write_text(content)
                http = FakeHttp()
                self.assertEqual(self.main("check", "--test", env=self.quiet, http=http, run=docker()), 0)
                self.assertEqual([(m["title"], m["message"]) for m in http.published()], [(
                    "TEST: Stack watch on zen box started over",
                    "check-test.json was unreadable, so it was moved aside to check-test.json.corrupt-20260915180000 "
                    "and the watch started fresh. An alert it already sent may repeat.")])
                aside = self.dir / "check-test.json.corrupt-20260915180000"
                self.assertEqual(aside.read_text(), content)
                aside.unlink()
                self.assertIn("tracked", self.saved("check-test.json"))

    def test_state_that_cannot_be_saved_sends_nothing(self):
        """Otherwise every run repeats the same alerts and heartbeat, and the ntfy budget floods."""
        state_dir = self.dir / "state"
        state_dir.mkdir()
        (state_dir / "check.json").write_text(json.dumps({"heartbeat": {"due": (NOW - MIN).isoformat()}}))
        http = FakeHttp()
        env = dict(self.env, STATE_DIR=str(state_dir), HEARTBEAT_GRACE="24h")
        state_dir.chmod(0o500)  # readable, but nothing can be written: like a full disk
        try:
            self.assertEqual(self.main("check", env=env, http=http, run=docker_down), 1)
            self.assertEqual(http.calls, [])
            self.assertEqual(self.main("disarm", env=env, http=http), 1)  # but disarm still cancels
            self.assertEqual(http.deletes(), ["https://ntfy.test/t/zen-box-heartbeat"])
        finally:
            state_dir.chmod(0o700)

    def test_a_disk_that_fills_mid_run_is_reported_instead_of_crashing(self):
        """The probe at the top proved the file was writable before anything was sent, so a failure
        at the final save means the disk filled during the run. Everything the run decided is lost —
        what it showed, the heartbeat it scheduled, and the failure counter that would say so — and
        it used to leave through an uncaught OSError with no journal line naming the state file."""
        real, calls = sw.save_json, []

        def fills_after_the_probe(path, data):
            calls.append(path)
            if len(calls) > 1:
                raise OSError(28, "No space left on device")
            real(path, data)

        http = FakeHttp()
        with unittest.mock.patch.object(sw, "save_json", fills_after_the_probe):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                code = sw.main(["check"], self.quiet, http, docker_down, NOW)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("may repeat", err.getvalue())
        # Nothing is notified from here: suppressing a repeat needs the state that cannot be saved.
        self.assertEqual(http.published(), [])

    def test_an_arr_switched_off_is_not_a_daily_failure(self):
        """A stack that doesn't run one of the arrs has no config.xml for it. Without an off switch
        that is a WatchError every day, and the only way to stop it is to turn the digest off."""
        for name in ("radarr", "sonarr"):
            (self.dir / "config" / name / "config.xml").unlink()
        env = dict(self.env, SONARR_URL="off", RADARR_URL="OFF")
        http = FakeHttp()
        self.assertEqual(self.main("stalled", env=env, http=http), 0)
        self.assertEqual((http.calls, http.published()), ([], []))
        self.assertEqual(self.saved("stalled.json"), {"notified": {}})

    def test_only_the_arr_switched_off_is_skipped(self):
        (self.dir / "config" / "sonarr" / "config.xml").unlink()
        http = FakeHttp({"/api/v3/": paged([])})
        self.assertEqual(self.main("stalled", env=dict(self.env, SONARR_URL="off"), http=http), 0)
        self.assertTrue(all("/api/v3/" in url for _, url, _, _ in http.calls))
        # Radarr is still reached, so switching one off does not quietly switch the digest off.
        self.assertTrue(http.calls)

    def test_describe_failure_never_uses_the_message(self):
        self.assertEqual(sw.describe_failure(OSError(28, "SENTINEL_SECRET")), "OSError (No space left on device)")
        self.assertEqual(sw.describe_failure(ValueError("SENTINEL_SECRET")), "ValueError")
        self.assertEqual(sw.describe_failure(sw.WatchError("radarr: HTTP 401")), "radarr: HTTP 401")

    def test_disarm_cancels_the_heartbeat_and_forgets_it(self):
        (self.dir / "check-test.json").write_text(json.dumps(
            {"heartbeat": {"due": NOW.isoformat(), "last": NOW.isoformat()}, "tracked": {}}))
        http = FakeHttp()
        self.assertEqual(self.main("disarm", "--test", http=http), 0)
        self.assertEqual(self.saved("check-test.json"), {"tracked": {}})
        self.assertEqual(http.deletes(), ["https://ntfy.test/t/zen-box-test-heartbeat"])

    def test_disarm_without_a_heartbeat_still_cancels(self):
        """The uninstaller relies on this when state was lost."""
        http = FakeHttp()
        self.assertEqual(self.main("disarm", http=http), 0)
        self.assertEqual(http.deletes(), ["https://ntfy.test/t/zen-box-heartbeat"])

    def test_dry_run_never_cancels_the_real_heartbeat(self):
        armed = json.dumps({"heartbeat": {"due": (NOW + HOUR).isoformat(), "last": NOW.isoformat()}})
        (self.dir / "check.json").write_text(armed)
        for argv, env in ((["disarm", "--dry-run"], self.env), (["check", "--dry-run"], self.quiet)):
            with self.subTest(argv=argv):
                http = FakeHttp()
                self.assertEqual(self.main(*argv, env=env, http=http, run=docker()), 0)
                self.assertEqual(http.calls, [])
                self.assertEqual((self.dir / "check.json").read_text(), armed)

    def test_test_mode_isolates_titles_state_sequence_ids_and_timing(self):
        env = dict(self.env, HEARTBEAT_GRACE="30s", JELLYFIN_URL="off")
        http = FakeHttp()
        self.assertEqual(self.main("check", "--test", env=env, http=http, run=docker_down), 0)
        sent = http.published()
        self.assertEqual((sent[0]["title"], sent[0]["sequence_id"]),
                         ("TEST: zen box is unreachable", "zen-box-test-heartbeat"))
        self.assertTrue((self.dir / "check-test.json").exists())
        self.assertFalse((self.dir / "check.json").exists())
        self.assertEqual(self.main("check", env=env, http=FakeHttp(), run=docker_down), 1)  # 30s is test-only

    def test_dry_run_sends_and_saves_nothing(self):
        http = FakeHttp()
        env = dict(self.env, HEARTBEAT_GRACE="1h", JELLYFIN_URL="off")
        self.assertEqual(self.main("check", "--dry-run", env=env, http=http, run=docker_down), 0)
        self.assertEqual(http.calls, [])
        self.assertFalse((self.dir / "check.json").exists())


def default_timeout(function):
    return inspect.signature(function).parameters["timeout"].default


# Read before any test patches these names.
HTTP_TIMEOUT = default_timeout(sw.http_request)
RUN_TIMEOUT = default_timeout(sw.run_cmd)
DISK_TIMEOUT = default_timeout(sw.filesystem_free)


class RunTimeBudgetTest(unittest.TestCase):
    """A run that outlives the unit's TimeoutStartSec is SIGTERMed with nothing it decided saved: no
    alert recorded as shown, no heartbeat recorded as rescheduled, no failure counted. So the longest
    a run can take has to fit inside it, with room for what no call is charged for.

    This used to be a hand-written sum, and it was wrong: it counted three ntfy publishes where one
    check can make four ("back online" and the reschedule, then "working again"), and nothing would
    have told whoever added the next check that the sum no longer held. It is derived instead.
    main() is driven through every combination of the state a run can start from and the one call
    it fails at, and every external call is charged the whole timeout the code gives it, as if each
    one hung until the last moment. A raised timeout, or a new call or publish on a path these
    scenarios reach, lands in the sum without anyone having to remember it. A check behind a new
    setting, or behind a container state the fake stack never produces, does not: add it to the
    scenarios along with the check.

    A charge is only a bound if the timeout is a ceiling; HttpRequestTest is what makes it one for
    http_request. docs/stack-watch.md quotes the results, and is checked against them here."""

    # Python starting, .env and state read and written, threads started: no call is charged for these.
    OVERHEAD = 30
    STALLED_PAGES = 1  # per list; each further page is one more HTTP_TIMEOUT, which the docs say

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.env = {"STACK_DIR": self.tmp.name, "STATE_DIR": self.tmp.name, "CONFIG_ROOT": self.tmp.name,
                    "NTFY_TOPIC": "t", "NTFY_SERVER": "https://ntfy.test", "WATCH_HOSTNAME": "h",
                    "JELLYFIN_URL": "http://jf", "MEDIA_ROOT": "/lib", "SABNZBD_TEMP": "/dl"}
        for name in ("radarr", "sonarr"):
            conf = self.dir / "config" / name
            conf.mkdir(parents=True)
            conf.joinpath("config.xml").write_text("<Port>1</Port><ApiKey>k</ApiKey>")

    def drive(self, command, state, fail_at, env=None):
        """Runs `command` once from `state` (a dict, or text for a file that isn't one), failing the
        `fail_at`th external call. Returns (seconds charged, titles published, calls made)."""
        charged, titles, unexpected = [], [], []
        stack = seerr_stack(container("sonarr", "exited", exit_code=1), container("seerr"))

        def charge(timeout, failure):
            charged.append(timeout)
            if len(charged) - 1 == fail_at:
                raise failure

        def http(method, url, headers=None, data=None, timeout=HTTP_TIMEOUT):
            charge(timeout, urllib.error.URLError("down"))
            if url.startswith("https://ntfy.test/"):
                if method == "POST":
                    titles.append(json.loads(data)["title"])
                return ""
            if url.endswith("/health"):
                return "Healthy"
            if "/api/v3/" in url:
                return json.dumps(paged([movie(), episode()]))
            unexpected.append(url)
            raise AssertionError(url)

        def run(argv, timeout=RUN_TIMEOUT):
            charge(timeout, RuntimeError("down"))
            return stack(argv)

        def free(path, timeout=DISK_TIMEOUT, read=None):
            charge(timeout, OSError(5, "Input/output error"))
            return 1, 500 * GB, 1000 * GB

        path = self.dir / f"{command}.json"
        path.write_text(state if isinstance(state, str) else json.dumps(state))
        with unittest.mock.patch.object(sw, "filesystem_free", free), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            sw.main([command], dict(self.env, **(env or {})), http, run, NOW)
        self.assertEqual(unexpected, [])  # a fake that fails the run would make the search vacuous
        return sum(charged), titles, len(charged)

    def worst(self, command, states, envs):
        """The most any run from these states can be charged, and every title any of them sent.
        Failing a call can add calls (a failed publish fails the run, which sends "is failing"), so
        the count isn't taken from the run where nothing fails: each index is tried until one is past
        the last call that run made, at which point nothing failed and later indices can't either."""
        worst, titles = 0, set()
        for state in states:
            for env in envs:
                for fail_at in itertools.count():
                    seconds, published, calls = self.drive(command, state, fail_at, env)
                    worst, titles = max(worst, seconds), titles | set(published)
                    if fail_at >= calls:
                        break
        return worst, titles

    def failure_states(self):
        since = iso(NOW - 15 * MIN)
        return [None, {"count": 1, "since": since},
                {"count": 1, "since": since, "alerted": iso(NOW - 2 * DAY)}]

    def test_a_check_fits_inside_the_units_start_timeout(self):
        seen_once = {"service:sonarr": {"count": 1, "first": iso(NOW - 15 * MIN), "seen": iso(NOW - 15 * MIN),
                                        "alerted": False, "description": "sonarr: exited (code 1)",
                                        "absent": 0}}
        overdue = {"due": iso(NOW - HOUR), "last": iso(NOW - 2 * DAY)}
        states = ["not json"] + [
            {key: value for key, value in (("failure", failure), ("tracked", tracked), ("heartbeat", beat))
             if value is not None}
            for failure in self.failure_states() for tracked in (None, seen_once) for beat in (None, overdue)]
        worst, titles = self.worst("check", states, [{}, {"HEARTBEAT_GRACE": "off"}])
        # Every notification a check can send was actually reached, so the search isn't vacuous.
        self.assertEqual(titles, {"Stack watch on h started over", "Media stack on h: 1 problem",
                                  "h is back online", "h is unreachable",
                                  "Stack watch on h is working again", "Stack watch on h is failing"})
        self.assertLessEqual(worst + self.OVERHEAD, unit_start_timeout(),
                             f"a check can take {worst}s against a {unit_start_timeout()}s limit")
        self.assertIn(f"a check at {worst} seconds", self.docs())

    def test_a_stalled_run_fits_inside_the_units_start_timeout(self):
        states = ["not json"] + [{"failure": f} if f else {} for f in self.failure_states()]
        worst, titles = self.worst("stalled", states, [{}])
        self.assertEqual(titles, {"Stack watch on h started over", "2 wanted titles not downloaded after 3 days",
                                  "Stack watch on h is working again", "Stack watch on h is failing"})
        self.assertLessEqual(worst + self.OVERHEAD, unit_start_timeout(),
                             f"a stalled run can take {worst}s against a {unit_start_timeout()}s limit")
        self.assertIn(f"a stalled run at {worst} seconds, plus {HTTP_TIMEOUT} for every page", self.docs())

    def test_the_docs_quote_the_units_start_timeout(self):
        self.assertIn(f"TimeoutStartSec={unit_start_timeout() // 60}min", self.docs())

    def docs(self):
        text = (Path(sw.__file__).resolve().parent.parent / "docs" / "stack-watch.md").read_text()
        return " ".join(text.split())


if __name__ == "__main__":
    unittest.main()
