"""Tests for stack_watch.py. Run: python3 -m unittest discover -s monitoring"""
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
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
    ntfy publishes succeed, unless fail_posts_after of them already have."""

    def __init__(self, routes=None, fail_posts_after=None):
        self.routes = routes or {}
        self.fail_posts_after = fail_posts_after
        self.calls, self.sent = [], []

    def __call__(self, method, url, headers=None, data=None, timeout=20):
        self.calls.append((method, url, headers, data))
        if url.startswith("https://ntfy.test"):
            if method == "POST":
                if self.fail_posts_after is not None and len(self.sent) >= self.fail_posts_after:
                    raise urllib.error.URLError("ntfy down")
                self.sent.append(json.loads(data))
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


def container(service, status="running", health=None, exit_code=0, oneoff=False):
    state = {"Status": status, "ExitCode": exit_code}
    if health:
        state["Health"] = {"Status": health}
    labels = {"com.docker.compose.service": service}
    if oneoff:
        labels["com.docker.compose.oneoff"] = "True"
    return {"Config": {"Labels": labels}, "State": state}


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


class ScheduleTest(unittest.TestCase):
    def test_check_timer_runs_every_check_interval(self):
        installer = Path(sw.__file__).with_name("install-stack-watch.sh").read_text()
        minutes = re.findall(r"^OnCalendar=\*:0/(\d+)$", installer, re.MULTILINE)
        self.assertEqual([timedelta(minutes=int(m)) for m in minutes], [sw.CHECK_INTERVAL])


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

    def test_one_good_container_is_enough(self):
        containers = [container("sonarr", "exited"), container("sonarr", health="healthy")]
        self.assertEqual(sw.container_problems(["sonarr"], containers), {})


class JellyfinTest(unittest.TestCase):
    def test_states(self):
        self.assertIsNone(sw.jellyfin_problem("http://jf", FakeHttp({"/health": "Healthy\n"})))
        self.assertEqual(sw.jellyfin_problem("http://jf", FakeHttp({"/health": "Degraded"})),
                         "jellyfin: reports Degraded")
        self.assertEqual(sw.jellyfin_problem("http://jf", FakeHttp({"/health": "<html>SENTINEL_SECRET"})),
                         "jellyfin: unexpected answer from /health")
        refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        self.assertIn("not answering", sw.jellyfin_problem("http://jf", FakeHttp({"/health": refused})))
        http_error = urllib.error.HTTPError("http://jf/health", 503, "x", {}, None)
        self.assertEqual(sw.jellyfin_problem("http://jf", FakeHttp({"/health": http_error})),
                         "jellyfin: health check returned HTTP 503")


def free_space(*known):
    """A fake filesystem_free: (path, (device, bytes free)) pairs. Any other path is unreadable."""
    paths = dict(known)

    def reader(path):
        if path not in paths:
            raise OSError(2, "No such file or directory")
        return paths[path]
    return reader


GB = 1024 ** 3
DISK_CFG = {"MEDIA_ROOT": "/lib", "SABNZBD_TEMP": "/dl"}


class DiskTest(unittest.TestCase):
    def problems(self, minimum, library, downloads):
        reader = free_space(("/lib", library), ("/dl", downloads))
        return sw.disk_problems(DISK_CFG, minimum, reader)

    def test_quiet_while_there_is_room(self):
        self.assertEqual(self.problems(100 * GB, (1, 143 * GB), (1, 143 * GB)), {})
        self.assertEqual(self.problems(100 * GB, (1, 100 * GB), (1, 100 * GB)), {})

    def test_paths_that_share_a_filesystem_are_one_problem(self):
        self.assertEqual(self.problems(100 * GB, (1, 30 * GB), (1, 30 * GB)),
                         {"disk:library+downloads": "disk: under 50G free for the library and downloads"})

    def test_separate_filesystems_are_reported_separately(self):
        self.assertEqual(self.problems(100 * GB, (1, 30 * GB), (2, 500 * GB)),
                         {"disk:library": "disk: under 50G free for the library"})
        self.assertEqual(self.problems(100 * GB, (1, 500 * GB), (2, 5 * GB)),
                         {"disk:downloads": "disk: under 10G free for the downloads"})

    def test_it_reports_the_lowest_step_it_is_under(self):
        """The exact figure would differ on nearly every run, and republish the stack notification
        every hour as downloads nudged it. A step only changes when crossing one, which is news."""
        under = {free: self.problems(100 * GB, (1, free), (1, free))["disk:library+downloads"]
                 .removeprefix("disk: under ").removesuffix(" free for the library and downloads")
                 for free in (99 * GB, 50 * GB, 50 * GB - 1, 25 * GB, 25 * GB - 1,
                              10 * GB, 10 * GB - 1, 0)}
        self.assertEqual(list(under.values()),
                         ["100G", "100G", "50G", "50G", "25G", "25G", "10G", "10G"])

    def test_the_lowest_step_is_a_floor_not_a_promise_of_more(self):
        """Under the smallest step there is nothing smaller to report, so the text stops changing."""
        self.assertEqual(self.problems(100 * GB, (1, 1), (1, 1))["disk:library+downloads"],
                         "disk: under 10G free for the library and downloads")

    def test_a_path_that_is_not_set_is_not_checked(self):
        reader = free_space(("/dl", (1, 5 * GB)))
        self.assertEqual(sw.disk_problems({"SABNZBD_TEMP": "/dl"}, 100 * GB, reader),
                         {"disk:downloads": "disk: under 10G free for the downloads"})
        self.assertEqual(sw.disk_problems({"MEDIA_ROOT": "  "}, 100 * GB, reader), {})

    def test_an_unreadable_path_is_reported_not_raised(self):
        reader = free_space(("/dl", (1, 500 * GB)))
        with contextlib.redirect_stderr(io.StringIO()):
            problems = sw.disk_problems(DISK_CFG, 100 * GB, reader)
        self.assertEqual(problems, {"disk:library": "disk: can't read free space for the library path"})

    def test_an_unreadable_path_never_carries_the_os_error_text(self):
        def reader(path):
            raise OSError(13, "SENTINEL_SECRET")
        with contextlib.redirect_stderr(io.StringIO()):
            problems = sw.disk_problems(DISK_CFG, 100 * GB, reader)
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

    def test_the_default_leaves_room_to_act_above_sabnzbds_own_floor(self):
        """SABnzbd pauses at download_free (50G as shipped) without telling anyone why. The default
        has to clear that by more than the largest season measured on this stack's profile (27G)."""
        self.assertGreaterEqual(sw.parse_size(sw.DISK_FREE_MIN_DEFAULT), sw.parse_size("50G") + 27 * GB)


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
        stack = {"shown": {"a": "a: exited"}, "sent": []}
        self.assertEqual(self.publish({"a": "a: exited"}, stack), (stack, []))

    def test_all_clear_waits_an_hour_after_the_last_update(self):
        stack = {"shown": {"a": "a: exited"}, "sent": [(NOW - 59 * MIN).isoformat()]}
        self.assertEqual(self.publish({}, stack), (stack, []))
        new, sent = self.publish({}, stack, now=NOW + MIN)
        self.assertEqual([(m["title"], m["message"], m["priority"], m["tags"]) for m in sent],
                         [("Media stack on h: all clear", "Everything that was down is back up.", 2,
                           ["white_check_mark"])])
        self.assertEqual(new["shown"], {})

    def test_changed_description_updates_at_default_priority(self):
        stack = {"shown": {"a": "a: restarting", "b": "b: unhealthy"}, "sent": []}
        _, sent = self.publish({"a": "a: exited (code 1)", "b": "b: unhealthy"}, stack)
        self.assertEqual([(m["title"], m["message"], m["priority"]) for m in sent],
                         [("Media stack on h: 2 problems", "• a: exited (code 1)\n• b: unhealthy", 3)])

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
        self.assertEqual(beat, {"due": (NOW + 25 * HOUR).isoformat(), "last": NOW.isoformat()})
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
        for grace_text, grace in (("30m", 30 * MIN), ("6h", 6 * HOUR), ("24h", DAY), ("71h", 71 * HOUR)):
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
        _, sent = self.beat("24h", self.due_in(0 * MIN))
        self.assertEqual([m["title"] for m in sent], ["h is unreachable"])
        beat, sent = self.beat("24h", self.due_in(-MIN, last_ago=26 * HOUR))
        self.assertEqual([m["title"] for m in sent], ["h is back online", "h is unreachable"])
        self.assertNotIn("delay", sent[0])
        self.assertEqual((sent[0]["priority"], sent[0]["sequence_id"]), (3, "h-heartbeat"))
        self.assertEqual(sent[0]["message"], f"Out of contact from {sw.when(NOW - 26 * HOUR)} to {sw.when(NOW)}. "
                                             f"The unreachable alert went out {sw.when(NOW - MIN)}.")
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
        self.assertEqual(self.state["heartbeat"], {"due": (NOW + 25 * HOUR).isoformat(), "last": NOW.isoformat()})
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
        for grace in ("29m", "72h", "90", "SENTINEL_SECRET"):
            with self.subTest(grace=grace), self.assertRaises(sw.WatchError) as caught:
                self.beat(grace)
            self.assertEqual(str(caught.exception), "HEARTBEAT_GRACE must be 30m to 71h, or off")
        for grace in ("30m", "71h"):
            self.assertEqual(len(self.beat(grace)[1]), 1)
        with self.assertRaisesRegex(sw.WatchError, "^HEARTBEAT_GRACE must be 10s to 3d, or off$"):
            self.beat("9s", timing=sw.TEST_TIMING)

    def test_test_timing_reschedules_every_run_at_exactly_grace(self):
        beat, sent = self.beat("10s", self.due_in(5 * SEC), timing=sw.TEST_TIMING)
        self.assertEqual(sent[0]["delay"], str(int((NOW + 10 * SEC).timestamp())))
        self.assertTrue(sent[0]["message"].startswith(f"Its last check-in was {sw.when(NOW)}. It is"))
        beat, sent = self.beat("3d", timing=sw.TEST_TIMING)
        self.assertEqual(beat["due"], (NOW + 3 * DAY).isoformat())


class RunCheckTest(unittest.TestCase):
    def check(self, state, run, at, cfg=None, http=None):
        http = FakeHttp({"/health": "Healthy"}) if http is None else http
        cfg = {"STACK_DIR": "/stack", "JELLYFIN_URL": "http://jf", "HEARTBEAT_GRACE": "off"} | (cfg or {})
        sw.run_check(make_watch(cfg, http, run, NOW + at), state)
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
            (105, "Media stack on h: 1 problem", "• sonarr: exited (code 1)"),
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
        """A threshold above any real drive, so the check reports the filesystem it is run on."""
        state, cfg = {}, {"MEDIA_ROOT": "/", "DISK_FREE_MIN": "1024T"}
        healthy = docker(container("sonarr"), container("radarr"))
        self.assertEqual(self.check(state, healthy, 0 * MIN, cfg), [])
        [sent] = self.check(state, healthy, 15 * MIN, cfg)
        self.assertEqual(sent["title"], "Media stack on h: 1 problem")
        self.assertRegex(sent["message"], r"^• disk: under [\d.]+[MGT] free for the library$")
        self.assertNotIn("/", sent["message"])  # the path names the user's home; the topic is public

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
        for name in ("radarr", "sonarr"):
            conf = self.dir / "config" / name
            conf.mkdir(parents=True)
            conf.joinpath("config.xml").write_text("<Port>1</Port><ApiKey>k</ApiKey>")

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *argv, env=None, http=None, run=None, now=NOW):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sw.main(list(argv), self.env if env is None else env,
                           FakeHttp() if http is None else http, run, now)

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


if __name__ == "__main__":
    unittest.main()
