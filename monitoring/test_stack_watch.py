"""Tests for stack_watch.py. Run: python3 -m unittest discover -s monitoring"""
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stack_watch as sw  # noqa: E402

NOW = datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


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
    """Routes by URL substring; records every call."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, method, url, headers=None, data=None, timeout=20):
        self.calls.append((method, url, headers, data))
        if url.startswith("https://ntfy.test"):
            return "{}"
        for fragment, response in self.routes.items():
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response if isinstance(response, str) else json.dumps(response)
        raise AssertionError(f"unexpected request {url}")

    def published(self):
        return [json.loads(data) for method, url, _, data in self.calls
                if url.startswith("https://ntfy.test")]


def paged(records):
    return {"page": 1, "pageSize": 250, "totalRecords": len(records), "records": records}


class EnvFileTest(unittest.TestCase):
    def test_parses_quotes_comments_and_export(self):
        env = sw.parse_env_file(
            "# comment\nA=1\nexport B=two\nC='p$ss # not a comment'\nD=\"x\"\nE=val # note\n\nnoise\n")
        self.assertEqual(env, {"A": "1", "B": "two", "C": "p$ss # not a comment", "D": "x",
                               "E": "val"})

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

    def test_due_for_new_items_and_reminders(self):
        self.assertTrue(sw.digest_due([self.item], {}, NOW, 7))
        recent = {"radarr:1": (NOW - timedelta(days=7) + timedelta(seconds=1)).isoformat()}
        self.assertFalse(sw.digest_due([self.item], recent, NOW, 7))
        old = {"radarr:1": (NOW - timedelta(days=7)).isoformat()}
        self.assertTrue(sw.digest_due([self.item], old, NOW, 7))

    def test_format_groups_episodes_and_marks_new(self):
        since = NOW - timedelta(days=4)
        items = [sw.Stalled(f"sonarr:{n}", "Big Show", f"S01E0{n}", since) for n in range(1, 6)]
        items += [sw.Stalled("sonarr:20", "Small Show", "S02E01", since),
                  sw.Stalled("sonarr:21", "Small Show", "S02E02", since), self.item]
        notified = {f"sonarr:{n}": NOW.isoformat() for n in range(1, 6)} | {
            "sonarr:20": NOW.isoformat(), "radarr:1": NOW.isoformat()}
        title, message = sw.format_digest(items, notified, 3)
        self.assertEqual(title, "3 wanted titles not downloaded after 3 days")
        lines = message.splitlines()
        self.assertTrue(lines[0].startswith("• Big Show: 5 episodes, waiting since "))
        self.assertFalse(lines[0].endswith("(new)"))
        self.assertTrue(lines[1].startswith("• Film (2024), waiting since"))
        self.assertTrue(lines[2].startswith("• Small Show: S02E01, S02E02, waiting since "))
        self.assertTrue(lines[2].endswith("(new)"))
        self.assertEqual(sw.format_digest([self.item], {}, 1)[0],
                         "1 wanted title not downloaded after 1 days")


class ArrTest(unittest.TestCase):
    def test_reads_port_urlbase_key_and_pages(self):
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d, "config", "radarr")
            conf.mkdir(parents=True)
            conf.joinpath("config.xml").write_text(
                "<Config><Port>7878</Port><UrlBase>/radarr</UrlBase><ApiKey>k123</ApiKey></Config>")
            http = FakeHttp()
            pages = iter([{"totalRecords": 3, "pageSize": 2, "records": [{"id": 1}, {"id": 2}]},
                          {"totalRecords": 3, "pageSize": 2, "records": [{"id": 3}]}])
            http.routes = {"wanted/missing": None}
            http.__class__ = type("PagingHttp", (FakeHttp,), {
                "__call__": lambda self, m, url, h=None, data=None, t=20: (
                    self.calls.append((m, url, h, data)) or json.dumps(next(pages)))})
            arr = sw.Arr.from_config({"CONFIG_ROOT": d}, "radarr", http)
            records = arr.all_records("wanted/missing", monitored=True)
            self.assertIsNone(sw.Arr.from_config({"CONFIG_ROOT": d}, "sonarr", http))
        self.assertEqual([r["id"] for r in records], [1, 2, 3])
        first_url, headers = http.calls[0][1], http.calls[0][2]
        self.assertTrue(first_url.startswith("http://localhost:7878/radarr/api/v3/wanted/missing?"))
        self.assertIn("monitored=true", first_url)
        self.assertIn("page=2", http.calls[1][1])
        self.assertEqual(headers, {"X-Api-Key": "k123"})

    def test_url_override(self):
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d, "config", "sonarr")
            conf.mkdir(parents=True)
            conf.joinpath("config.xml").write_text("<Port>8989</Port><ApiKey>k</ApiKey>")
            arr = sw.Arr.from_config({"CONFIG_ROOT": d, "SONARR_URL": "http://nas:1/s/"}, "sonarr", None)
        self.assertEqual(arr.base, "http://nas:1/s")


class RunStalledTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name, port in (("radarr", 7878), ("sonarr", 8989)):
            conf = Path(self.tmp.name, "config", name)
            conf.mkdir(parents=True)
            conf.joinpath("config.xml").write_text(f"<Port>{port}</Port><ApiKey>k</ApiKey>")
        self.cfg = {"CONFIG_ROOT": self.tmp.name, "STALL_DAYS": "3"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, state, movies, now=NOW, queue=()):
        http = FakeHttp({"7878/api/v3/queue": paged([{"movieId": q} for q in queue]),
                         "7878/api/v3/wanted/missing": paged(movies),
                         "8989/api/v3/queue": paged([]),
                         "8989/api/v3/wanted/missing": paged([episode()])})
        notifier = sw.Notifier("https://ntfy.test", "topic", http)
        sw.run_stalled(self.cfg, notifier, state, now, http, None, "host", "host")
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

        self.assertEqual(self.run_once(state, [movie()], now=NOW + timedelta(days=1)), [])
        self.assertEqual(self.run_once(state, [movie()], now=NOW + timedelta(days=7, seconds=-1)), [])
        self.assertEqual(len(self.run_once(state, [movie()], now=NOW + timedelta(days=7))), 1)

        self.assertEqual(self.run_once(state, [], now=NOW + timedelta(days=8)), [])
        self.assertEqual(set(state["notified"]), {"sonarr:11"})

    def test_new_item_resends_full_list(self):
        state = {}
        self.run_once(state, [movie()])
        again = self.run_once(state, [movie(), movie(id=2, title="Other")], now=NOW + timedelta(hours=1))
        self.assertEqual(len(again), 1)
        self.assertIn("Other (2024), waiting since", again[0]["message"])
        self.assertIn("(new)", again[0]["message"])

    def test_queued_movie_is_not_stalled(self):
        state = {}
        sent = self.run_once(state, [movie()], queue=[1])
        self.assertNotIn("Film", sent[0]["message"])


def container(service, status="running", health=None, exit_code=0, oneoff=False):
    state = {"Status": status, "ExitCode": exit_code}
    if health:
        state["Health"] = {"Status": health}
    labels = {"com.docker.compose.service": service}
    if oneoff:
        labels["com.docker.compose.oneoff"] = "True"
    return {"Config": {"Labels": labels}, "State": state}


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
                         "jellyfin: reports 'Degraded'")
        refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        self.assertIn("not answering", sw.jellyfin_problem("http://jf", FakeHttp({"/health": refused})))
        http_error = urllib.error.HTTPError("http://jf/health", 503, "x", {}, None)
        self.assertEqual(sw.jellyfin_problem("http://jf", FakeHttp({"/health": http_error})),
                         "jellyfin: health check returned HTTP 503")


class AdvanceTest(unittest.TestCase):
    def test_debounce_alert_steady_and_recovery(self):
        tracked, changed, worsened = sw.advance({}, {"a": "a: exited"})
        self.assertEqual((changed, worsened, tracked["a"]["alerted"]), (False, False, False))
        tracked, changed, worsened = sw.advance(tracked, {"a": "a: exited"})
        self.assertEqual((changed, worsened, tracked["a"]["alerted"]), (True, True, True))
        tracked, changed, worsened = sw.advance(tracked, {"a": "a: exited"})
        self.assertEqual((changed, worsened), (False, False))
        tracked, changed, worsened = sw.advance(tracked, {})
        self.assertEqual((tracked, changed, worsened), ({}, True, False))

    def test_blip_that_clears_before_threshold_is_silent(self):
        tracked, _, _ = sw.advance({}, {"a": "x"})
        tracked, changed, _ = sw.advance(tracked, {})
        self.assertEqual((tracked, changed), ({}, False))

    def test_format(self):
        tracked = {"b": {"alerted": True, "description": "b: x"}, "a": {"alerted": True, "description": "a: y"},
                   "c": {"alerted": False, "description": "c: z"}}
        self.assertEqual(sw.format_check(tracked, "h", True),
                         ("Media stack on h: 2 problems", "• a: y\n• b: x", 4))
        self.assertEqual(sw.format_check({"a": tracked["a"]}, "h", False)[::2],
                         ("Media stack on h: 1 problem", 3))
        self.assertEqual(sw.format_check({}, "h", False)[::2], ("Media stack on h: all clear", 2))


class HeartbeatTest(unittest.TestCase):
    def send(self, grace, previous, now=NOW):
        http = FakeHttp()
        notifier = sw.Notifier("https://ntfy.test", "topic", http)
        new = sw.heartbeat({"HEARTBEAT_GRACE": grace}, notifier, previous, now, "zen", "zen")
        return new, http.published()

    def test_schedules_replaceable_alert(self):
        new, sent = self.send("24h", {})
        self.assertEqual(new, {"last": NOW.isoformat()})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["sequence_id"], "zen-heartbeat")
        self.assertEqual(sent[0]["delay"], str(int((NOW + timedelta(hours=24)).timestamp())))
        self.assertEqual((sent[0]["title"], sent[0]["priority"]), ("zen is unreachable", 4))

    def test_recovery_only_after_gap_longer_than_grace(self):
        at_grace = {"last": (NOW - timedelta(hours=24)).isoformat()}
        self.assertEqual(len(self.send("24h", at_grace)[1]), 1)
        past_grace = {"last": (NOW - timedelta(hours=24, seconds=1)).isoformat()}
        _, sent = self.send("24h", past_grace)
        self.assertEqual([m["title"] for m in sent], ["zen is back online", "zen is unreachable"])
        self.assertNotIn("delay", sent[0])
        self.assertEqual(sent[0]["sequence_id"], "zen-heartbeat")

    def test_off_and_limits(self):
        self.assertEqual(self.send("off", {"last": "x"}), ({}, []))
        for grace in ("9s", "4d"):
            with self.assertRaises(ValueError):
                self.send(grace, {})


class RunCheckTest(unittest.TestCase):
    def test_docker_down_alerts_after_two_runs_heartbeat_every_run(self):
        def docker_down(argv):
            raise RuntimeError("Cannot connect to the Docker daemon")
        cfg = {"STACK_DIR": "/stack", "JELLYFIN_URL": "http://jf", "HEARTBEAT_GRACE": "1h"}
        state, runs = {}, []
        for minutes in (0, 15):
            http = FakeHttp({"/health": "Healthy"})
            notifier = sw.Notifier("https://ntfy.test", "topic", http)
            sw.run_check(cfg, notifier, state, NOW + timedelta(minutes=minutes), http, docker_down, "h", "h")
            runs.append(http.published())
        self.assertEqual([m["title"] for m in runs[0]], ["h is unreachable"])
        self.assertEqual([m["title"] for m in runs[1]], ["Media stack on h: 1 problem", "h is unreachable"])
        self.assertEqual(runs[1][0]["sequence_id"], "h-stack")
        self.assertIn("docker: not responding (Cannot connect to the Docker daemon)", runs[1][0]["message"])

    def test_compose_state_feeds_container_problems(self):
        outputs = {"config": json.dumps({"name": "proj", "services": {"sonarr": {}, "radarr": {}}}),
                   "ps": "abc\ndef\n",
                   "inspect": json.dumps([container("sonarr"), container("radarr", "exited", exit_code=1)])}
        seen = []

        def run(argv):
            seen.append(argv)
            return outputs[argv[1] if argv[1] != "compose" else "config"]
        services, containers = sw.compose_state(Path("/stack"), run)
        self.assertEqual(services, ["radarr", "sonarr"])
        self.assertIn("label=com.docker.compose.project=proj", seen[1])
        self.assertEqual(seen[2], ["docker", "inspect", "abc", "def"])
        self.assertEqual(sw.container_problems(services, containers), {"service:radarr": "radarr: exited (code 1)"})

    def test_failed_send_does_not_advance_state(self):
        state = {"tracked": {"jellyfin": {"count": 1, "alerted": False, "description": "x"}}}

        def http(method, url, headers=None, data=None, timeout=20):
            if "/health" in url:
                raise urllib.error.URLError("refused")
            raise urllib.error.URLError("ntfy down")
        notifier = sw.Notifier("https://ntfy.test", "topic", http)
        cfg = {"STACK_DIR": "/s", "JELLYFIN_URL": "http://jf", "HEARTBEAT_GRACE": "off"}
        with self.assertRaises(urllib.error.URLError):
            sw.run_check(cfg, notifier, state, NOW, http, lambda a: (_ for _ in ()).throw(RuntimeError("x")), "h", "h")
        self.assertEqual(state["tracked"]["jellyfin"]["count"], 1)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"STACK_DIR": self.tmp.name, "STATE_DIR": self.tmp.name, "NTFY_TOPIC": "t",
                    "NTFY_SERVER": "https://ntfy.test", "WATCH_HOSTNAME": "zen box",
                    "CONFIG_ROOT": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_topic(self):
        env = dict(self.env, NTFY_TOPIC="")
        self.assertEqual(sw.main(["check"], env, FakeHttp(), None, NOW), 2)

    def test_repeated_failure_alerts_once_then_recovers(self):
        broken = FakeHttp({"/api/v3/": urllib.error.URLError("refused")})
        conf = Path(self.tmp.name, "config", "radarr")
        conf.mkdir(parents=True)
        conf.joinpath("config.xml").write_text("<Port>1</Port><ApiKey>k</ApiKey>")
        codes = [sw.main(["stalled"], self.env, broken, None, NOW + timedelta(days=d)) for d in range(3)]
        self.assertEqual(codes, [1, 1, 1])
        failing = broken.published()
        self.assertEqual([m["title"] for m in failing], ["Stack watch on zen box is failing"])
        self.assertEqual(failing[0]["sequence_id"], "zen-box-watch-stalled")
        self.assertIn("failed 2 runs in a row", failing[0]["message"])

        fixed = FakeHttp({"/api/v3/": paged([])})
        self.assertEqual(sw.main(["stalled"], self.env, fixed, None, NOW + timedelta(days=3)), 0)
        self.assertEqual([m["title"] for m in fixed.published()], ["Stack watch on zen box is working again"])
        self.assertEqual(sw.load_json(Path(self.tmp.name, "stalled.json")), {"notified": {}})

    def test_single_failure_then_success_is_silent(self):
        conf = Path(self.tmp.name, "config", "radarr")
        conf.mkdir(parents=True)
        conf.joinpath("config.xml").write_text("<Port>1</Port><ApiKey>k</ApiKey>")
        broken = FakeHttp({"/api/v3/": urllib.error.URLError("refused")})
        self.assertEqual(sw.main(["stalled"], self.env, broken, None, NOW), 1)
        fixed = FakeHttp({"/api/v3/": paged([])})
        self.assertEqual(sw.main(["stalled"], self.env, fixed, None, NOW), 0)
        self.assertEqual(broken.published() + fixed.published(), [])

    def test_undelivered_failure_alert_is_retried(self):
        conf = Path(self.tmp.name, "config", "radarr")
        conf.mkdir(parents=True)
        conf.joinpath("config.xml").write_text("<Port>1</Port><ApiKey>k</ApiKey>")

        def all_down(method, url, headers=None, data=None, timeout=20):
            raise urllib.error.URLError("offline")
        for _ in range(3):
            sw.main(["stalled"], self.env, all_down, None, NOW)
        state = sw.load_json(Path(self.tmp.name, "stalled.json"))
        self.assertEqual((state["failures"], state.get("failure_alerted")), (3, None))
        recovering = FakeHttp({"/api/v3/": urllib.error.URLError("still refused")})
        sw.main(["stalled"], self.env, recovering, None, NOW)
        self.assertEqual(len(recovering.published()), 1)

    def test_test_mode_isolates_titles_state_and_sequence_ids(self):
        env = dict(self.env, HEARTBEAT_GRACE="30s", JELLYFIN_URL="off")
        run = lambda argv: (_ for _ in ()).throw(RuntimeError("no docker"))  # noqa: E731
        http = FakeHttp()
        self.assertEqual(sw.main(["check", "--test"], env, http, run, NOW), 0)
        sent = http.published()
        self.assertEqual(sent[0]["title"], "TEST: zen box is unreachable")
        self.assertEqual(sent[0]["sequence_id"], "zen-box-test-heartbeat")
        self.assertTrue(Path(self.tmp.name, "check-test.json").exists())
        self.assertFalse(Path(self.tmp.name, "check.json").exists())

    def test_dry_run_sends_and_saves_nothing(self):
        env = dict(self.env, HEARTBEAT_GRACE="1h", JELLYFIN_URL="off")
        run = lambda argv: (_ for _ in ()).throw(RuntimeError("no docker"))  # noqa: E731
        http = FakeHttp()
        self.assertEqual(sw.main(["check", "--dry-run"], env, http, run, NOW), 0)
        self.assertEqual(http.calls, [])
        self.assertFalse(Path(self.tmp.name, "check.json").exists())


if __name__ == "__main__":
    unittest.main()
