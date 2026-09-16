"""Tests for jellyfin-refresh-series.sh. Run: python3 -m unittest discover -s scripts

The script is exercised as a black box: a stub `curl` earlier on PATH answers from a scripted
table and records every invocation, so each test asserts on the calls the script actually made
rather than on its internals. `sleep` is stubbed to return instantly, which is what keeps a
suite that exercises a 60s delay and a 180s poll loop fast enough to run on every change.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "jellyfin-refresh-series.sh"

# A stub curl. Matches the request against a list of rules in $STUB_PLAN and replies with the
# first one that matches, or 404 if none do. Mirrors the real curl's `-w '\n%{http_code}'`
# contract: body, newline, status. Records each call as one JSON line in $STUB_CALLS.
STUB_CURL = r'''#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
method = "GET"
url = ""
for i, a in enumerate(argv):
    if a == "-X":
        method = argv[i + 1]
    elif a.startswith("http"):
        url = a
headers = [argv[i + 1] for i, a in enumerate(argv) if a == "-H"]

with open(os.environ["STUB_CALLS"], "a") as fh:
    fh.write(json.dumps({"method": method, "url": url, "headers": headers}) + "\n")

plan = json.load(open(os.environ["STUB_PLAN"]))
state = json.load(open(os.environ["STUB_STATE"])) if os.path.exists(os.environ["STUB_STATE"]) else {}

for rule in plan:
    if rule.get("method", method) != method:
        continue
    if rule["match"] not in url:
        continue
    # A rule may fire only after another rule has been hit (models "heals after the refresh").
    need = rule.get("after")
    if need and not state.get(need):
        continue
    if rule.get("sets"):
        state[rule["sets"]] = True
        json.dump(state, open(os.environ["STUB_STATE"], "w"))
    body = rule.get("body", "")
    if not isinstance(body, str):
        body = json.dumps(body)
    code = rule.get("exit", 0)
    # Real curl writes %{http_code} as 000 when it never got a response at all (connection
    # refused, DNS failure), but as the REAL code when headers arrived before the failure (a
    # truncated body, a timeout mid-transfer). A rule with "exit" and no "status" models the
    # first; a rule with both models the second.
    status = str(rule["status"]) if "status" in rule else ("000" if code else "200")
    sys.stdout.write(body + "\n" + status)
    sys.exit(code)

sys.stdout.write("\n404")
'''

STUB_SLEEP = "#!/bin/sh\nexit 0\n"


# Fixture titles, ids and paths are made up. This repo is public: never use a real host's paths or
# library contents here. The Jellyfin path is a host path and the Sonarr one a container path, so
# they share only the series folder name -- which is all the script compares.
def series_item(item_id="ITEM1", name="Example Show", tvdb="100001", path="/srv/media/complete/tv/Example Show"):
    return {"Id": item_id, "Name": name, "Path": path,
            "ProviderIds": ({"Tvdb": tvdb} if tvdb else {})}


class ScriptRun:
    def __init__(self, returncode, stdout, stderr, calls, logfile):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = calls
        self.log = logfile

    @property
    def output(self):
        """Everything the run emitted, including the detached worker's log file."""
        return self.stdout + self.stderr + self.log

    def urls(self, method=None):
        return [c["url"] for c in self.calls if method is None or c["method"] == method]


class RefreshScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "curl").write_text(STUB_CURL)
        (self.bin / "curl").chmod(0o755)
        (self.bin / "sleep").write_text(STUB_SLEEP)
        (self.bin / "sleep").chmod(0o755)
        self.plan = self.tmp / "plan.json"
        self.calls = self.tmp / "calls.jsonl"
        self.state = self.tmp / "state.json"
        self.statedir = self.tmp / "state"

    def run_script(self, plan=None, env=None, child=True):
        self.plan.write_text(json.dumps(plan or []))
        self.calls.write_text("")
        e = dict(os.environ)
        e["PATH"] = f"{self.bin}:{e['PATH']}"
        e.update({
            "STUB_PLAN": str(self.plan), "STUB_CALLS": str(self.calls), "STUB_STATE": str(self.state),
            "JELLYFIN_URL": "http://jellyfin.test:8096", "JELLYFIN_API_KEY": "KEY",
            "STATE_DIR": str(self.statedir),
            # Zero deadlines make every give-up path take exactly one pass instead of spinning
            # against the wall clock -- `sleep` is stubbed out, so a real timeout would burn
            # that many seconds of CPU for no extra coverage.
            "REFRESH_DELAY": "0", "REFRESH_TIMEOUT": "0", "LOOKUP_TIMEOUT": "0",
        })
        if child:
            # Run the worker inline instead of letting it detach, so the test can see its exit
            # code. Dispatch itself is covered separately by test_download_dispatches_and_returns.
            e["REFRESH_CHILD"] = "1"
        e.update(env or {})
        p = subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True, timeout=120)
        calls = [json.loads(l) for l in self.calls.read_text().splitlines() if l.strip()]
        logf = self.statedir / "jellyfin-refresh.log"
        return ScriptRun(p.returncode, p.stdout, p.stderr, calls,
                         logf.read_text() if logf.exists() else "")

    def import_env(self, **over):
        base = {"sonarr_eventtype": "Download", "sonarr_series_id": "5",
                "sonarr_series_title": "Example Show", "sonarr_series_tvdbid": "100001",
                "sonarr_series_path": "/data/complete/tv/Example Show"}
        base.update(over)
        return base

    # --- plans -----------------------------------------------------------------

    def plan_stranded_then_healed(self, seasons_after=1):
        return [
            {"match": "/Items?IncludeItemTypes=Series", "body": {"Items": [series_item()]}},
            {"match": "/Refresh", "method": "POST", "status": 204, "body": "", "sets": "refreshed"},
            {"match": "/Shows/ITEM1/Seasons", "after": "refreshed",
             "body": {"TotalRecordCount": seasons_after}},
            {"match": "/Shows/ITEM1/Seasons", "body": {"TotalRecordCount": 0}},
        ]

    def plan_healthy(self):
        return [
            {"match": "/Items?IncludeItemTypes=Series", "body": {"Items": [series_item()]}},
            {"match": "/Shows/ITEM1/Seasons", "body": {"TotalRecordCount": 2}},
        ]

    # --- guard rails -----------------------------------------------------------

    def test_no_event_is_a_usage_error(self):
        r = self.run_script(env={"sonarr_eventtype": ""}, child=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("Custom Script connector", r.stderr)
        self.assertEqual(r.calls, [])

    def test_unrelated_event_does_nothing(self):
        r = self.run_script(env=self.import_env(sonarr_eventtype="Grab"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.calls, [], "an unrelated event must not touch Jellyfin")

    # --- the Test button -------------------------------------------------------

    def test_test_event_succeeds_when_jellyfin_answers(self):
        r = self.run_script([{"match": "/System/Info", "body": {"Version": "10.11.11"}}],
                            env={"sonarr_eventtype": "Test"}, child=False)
        self.assertEqual(r.returncode, 0)
        self.assertIn("test OK", r.stdout)

    def test_test_event_fails_on_empty_key(self):
        r = self.run_script(env={"sonarr_eventtype": "Test", "JELLYFIN_API_KEY": ""}, child=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("JELLYFIN_API_KEY is empty", r.stdout)
        self.assertEqual(r.calls, [], "must not call Jellyfin with no key")

    def test_test_event_fails_on_bad_key(self):
        r = self.run_script([{"match": "/System/Info", "status": 401, "body": ""}],
                            env={"sonarr_eventtype": "Test"}, child=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("401", r.stdout)

    # --- dispatch --------------------------------------------------------------

    def test_download_dispatches_and_returns_without_refreshing(self):
        """Sonarr waits on this script, so the foreground call must return before any work."""
        r = self.run_script(self.plan_stranded_then_healed(),
                            env=self.import_env(), child=False)
        self.assertEqual(r.returncode, 0)
        self.assertIn("dispatched background worker", r.stdout)
        self.assertEqual([c for c in r.calls if c["method"] == "POST"], [],
                         "the foreground call must not perform the refresh itself")

    # --- the fix ---------------------------------------------------------------

    def test_stranded_series_is_refreshed_and_confirmed_healed(self):
        r = self.run_script(self.plan_stranded_then_healed(), env=self.import_env())
        self.assertEqual(r.returncode, 0)
        posts = r.urls("POST")
        self.assertEqual(len(posts), 1, f"expected exactly one refresh, got {posts}")
        self.assertIn("/Items/ITEM1/Refresh", posts[0])
        self.assertIn("STRANDED", r.output)
        self.assertIn("HEALED", r.output)

    def test_refresh_requests_a_full_metadata_refresh(self):
        r = self.run_script(self.plan_stranded_then_healed(), env=self.import_env())
        self.assertIn("metadataRefreshMode=FullRefresh", r.urls("POST")[0],
                      "Default mode does not rewrite the stale key; FullRefresh is the fix")

    def test_refresh_does_not_send_a_recursive_parameter(self):
        """`recursive` is not a parameter of this endpoint on 10.11 -- see the script header.

        Pinned so that anyone 'restoring' it from the upstream issue or from our own older docs
        has to read why it was removed.
        """
        self.assertNotIn("recursive", r"".join(
            self.run_script(self.plan_stranded_then_healed(), env=self.import_env()).urls("POST")
        ).lower())

    def test_refresh_does_not_replace_existing_metadata(self):
        """replaceAllMetadata=true would discard artwork and overrides; we only need the re-join."""
        self.assertIn("replaceAllMetadata=false",
                      self.run_script(self.plan_stranded_then_healed(), env=self.import_env()).urls("POST")[0])

    # --- idempotency -----------------------------------------------------------

    def test_healthy_series_is_not_refreshed(self):
        r = self.run_script(self.plan_healthy(), env=self.import_env())
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.urls("POST"), [], "a series that already joins must be left alone")
        self.assertIn("no refresh needed", r.output)

    def test_second_worker_for_same_series_exits_without_refreshing(self):
        """A season pack fires this script once per file; only one may refresh."""
        self.statedir.mkdir(parents=True, exist_ok=True)
        lock = self.statedir / "5.lock"
        lock.touch()
        holder = subprocess.Popen(["flock", "-x", str(lock), "sleep", "10"])

        def stop():
            holder.kill()
            holder.wait()
        self.addCleanup(stop)
        # Block until the holder actually owns the lock, so this can't race.
        subprocess.run(["flock", "-w", "5", "-s", str(lock), "true"], check=False)
        r = self.run_script(self.plan_stranded_then_healed(), env=self.import_env())
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.urls("POST"), [], "the duplicate worker must not refresh")
        self.assertIn("skipping duplicate", r.output)

    # --- identification --------------------------------------------------------

    def test_matches_series_by_folder_when_not_yet_identified(self):
        """During the identification window the item has no provider ids at all."""
        plan = [
            {"match": "/Items?IncludeItemTypes=Series",
             "body": {"Items": [series_item(tvdb=None)]}},
            {"match": "/Refresh", "method": "POST", "status": 204, "body": "", "sets": "r"},
            {"match": "/Shows/ITEM1/Seasons", "after": "r", "body": {"TotalRecordCount": 1}},
            {"match": "/Shows/ITEM1/Seasons", "body": {"TotalRecordCount": 0}},
        ]
        r = self.run_script(plan, env=self.import_env())
        self.assertEqual(r.returncode, 0)
        self.assertIn("/Items/ITEM1/Refresh", r.urls("POST")[0])

    def test_ignores_a_different_series(self):
        plan = [
            {"match": "/Items?IncludeItemTypes=Series",
             "body": {"Items": [series_item(item_id="OTHER", name="Another Show", tvdb="100002",
                                            path="/srv/media/complete/tv/Another Show")]}},
        ]
        r = self.run_script(plan, env=self.import_env())
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.urls("POST"), [], "must not refresh an unrelated series")
        self.assertIn("is not in Jellyfin", r.output)

    def test_unreachable_jellyfin_is_not_reported_as_a_missing_series(self):
        """Three causes look identical from here; saying the wrong one sends you hunting."""
        r = self.run_script([{"match": "Items?IncludeItemTypes=Series", "exit": 7, "body": ""}],
                            env=self.import_env())
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not reach Jellyfin", r.output)
        self.assertNotIn("is not in Jellyfin", r.output)

    def lookup(self, **rule):
        rule.setdefault("body", "")
        return self.run_script([dict(match="Items?IncludeItemTypes=Series", **rule)],
                               env=self.import_env())

    def test_rejected_series_lookup_points_at_the_key(self):
        """Names the .env setting the operator actually edits, not the container's copy of it."""
        for status in (401, 403):
            with self.subTest(status=status):
                r = self.lookup(status=status)
                self.assertEqual(r.returncode, 1)
                self.assertIn("JELLYFIN_ARR_API_KEY", r.output)
                self.assertNotIn("is not in Jellyfin", r.output)

    def test_server_error_on_lookup_does_not_blame_the_key(self):
        """A 503 while Jellyfin starts up is not a credentials problem; saying so sends you hunting."""
        for status in (500, 503):
            with self.subTest(status=status):
                r = self.lookup(status=status)
                self.assertEqual(r.returncode, 1)
                self.assertIn(f"HTTP {status}", r.output)
                self.assertNotIn("API_KEY", r.output)
                self.assertNotIn("is not in Jellyfin", r.output)

    def test_not_found_lookup_points_at_the_address(self):
        """A 404 on /Items means the wrong server or base path, not a server still starting."""
        r = self.lookup(status=404)
        self.assertEqual(r.returncode, 1)
        self.assertIn("HTTP 404", r.output)
        self.assertIn("JELLYFIN_INTERNAL_URL", r.output)
        self.assertNotIn("starting up", r.output)
        self.assertNotIn("is not in Jellyfin", r.output)

    def test_series_listed_in_an_error_response_is_not_trusted(self):
        """A non-2xx body is not a library listing, even if it happens to parse as one."""
        for status in (302, 500):
            with self.subTest(status=status):
                r = self.run_script(
                    [{"match": "Items?IncludeItemTypes=Series", "status": status,
                      "body": {"Items": [series_item()]}},
                     {"match": "/Shows/ITEM1/Seasons", "body": {"TotalRecordCount": 0}},
                     {"match": "/Refresh", "method": "POST", "status": 204, "body": ""}],
                    env=self.import_env())
                self.assertEqual(r.returncode, 1)
                self.assertEqual(r.urls("POST"), [], "must not refresh on an error body")
                self.assertIn(f"HTTP {status}", r.output)

    def test_redirected_lookup_is_not_reported_as_a_missing_series(self):
        """Only a 2xx answer can say the series is absent; a 3xx never looked."""
        r = self.lookup(status=302)
        self.assertEqual(r.returncode, 1)
        self.assertIn("HTTP 302", r.output)
        self.assertNotIn("is not in Jellyfin", r.output)

    def test_transfer_that_fails_after_a_200_is_not_a_missing_series(self):
        """Real curl reports the real code when headers arrived before the failure (exit 18/28/56)."""
        for code in (18, 28, 56):
            with self.subTest(exit=code):
                r = self.lookup(status=200, exit=code, body='{"Items": [{"Id": "IT')
                self.assertEqual(r.returncode, 1)
                self.assertIn("failed partway", r.output)
                self.assertNotIn("is not in Jellyfin", r.output)

    def test_a_200_that_is_not_a_series_list_is_not_a_missing_series(self):
        """A login page or some other service answering 200 has not searched any library."""
        for body in ("<html><body>Sign in</body></html>", '{"Name": "not jellyfin"}'):
            with self.subTest(body=body):
                r = self.lookup(status=200, body=body)
                self.assertEqual(r.returncode, 1)
                self.assertIn("not a series list", r.output)
                self.assertIn("JELLYFIN_INTERNAL_URL", r.output)
                self.assertNotIn("is not in Jellyfin", r.output)

    def test_other_2xx_statuses_can_report_a_missing_series(self):
        r = self.lookup(status=203, body={"Items": []})
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not in Jellyfin", r.output)

    def test_import_complete_event_is_handled(self):
        r = self.run_script(self.plan_stranded_then_healed(),
                            env=self.import_env(sonarr_eventtype="ImportComplete"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(r.urls("POST")), 1)

    # --- failure reporting -----------------------------------------------------

    def test_refresh_that_never_heals_is_reported_as_failure(self):
        r = self.run_script(self.plan_stranded_then_healed(seasons_after=0), env=self.import_env())
        self.assertEqual(r.returncode, 1, "a silent non-heal is the failure mode that matters")
        self.assertIn("STILL STRANDED", r.output)

    def test_unreadable_seasons_after_refresh_are_not_called_stranded(self):
        """If Jellyfin stops answering after the POST, nobody knows whether it healed."""
        plan = [
            {"match": "/Items?IncludeItemTypes=Series", "body": {"Items": [series_item()]}},
            {"match": "/Refresh", "method": "POST", "status": 204, "body": "", "sets": "r"},
            {"match": "/Shows/ITEM1/Seasons", "after": "r", "status": 503, "body": ""},
            {"match": "/Shows/ITEM1/Seasons", "body": {"TotalRecordCount": 0}},
        ]
        r = self.run_script(plan, env=self.import_env())
        self.assertEqual(r.returncode, 1)
        self.assertIn("HTTP 503", r.output)
        self.assertNotIn("STILL STRANDED", r.output)

    def test_rejected_refresh_is_reported(self):
        plan = [
            {"match": "/Items?IncludeItemTypes=Series", "body": {"Items": [series_item()]}},
            {"match": "/Shows/ITEM1/Seasons", "body": {"TotalRecordCount": 0}},
            {"match": "/Refresh", "method": "POST", "status": 403, "body": ""},
        ]
        r = self.run_script(plan, env=self.import_env())
        self.assertEqual(r.returncode, 1)
        self.assertIn("REJECTED", r.output)

    def test_unauthorized_season_lookup_is_not_read_as_zero_seasons(self):
        """A 401 body has no TotalRecordCount; parsing it as 0 would look like stranding."""
        plan = [
            {"match": "/Items?IncludeItemTypes=Series", "body": {"Items": [series_item()]}},
            {"match": "/Shows/ITEM1/Seasons", "status": 401, "body": ""},
            {"match": "/Refresh", "method": "POST", "status": 401, "body": ""},
        ]
        r = self.run_script(plan, env=self.import_env())
        self.assertIn("could not read season count", r.output)
        self.assertNotIn("is STRANDED", r.output)
        self.assertEqual(r.returncode, 1)

    def test_missing_api_key_on_import_is_reported(self):
        r = self.run_script(self.plan_stranded_then_healed(),
                            env=dict(self.import_env(), JELLYFIN_API_KEY=""))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.calls, [])
        self.assertIn("JELLYFIN_API_KEY is not set", r.output)

    # --- dry run ---------------------------------------------------------------

    def test_dry_run_detects_but_does_not_refresh(self):
        r = self.run_script(self.plan_stranded_then_healed(),
                            env=dict(self.import_env(), REFRESH_DRYRUN="1"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.urls("POST"), [])
        self.assertIn("dry run", r.output)
        self.assertIn("STRANDED", r.output)

    def test_dry_run_describes_exactly_the_call_it_would_make(self):
        """Otherwise the dry run is worse than nothing -- it reports a call that isn't the one."""
        dry = self.run_script(self.plan_stranded_then_healed(),
                              env=dict(self.import_env(), REFRESH_DRYRUN="1"))
        described = dry.output.split("would POST ", 1)[1].split()[0].lstrip("/")
        real = self.run_script(self.plan_stranded_then_healed(), env=self.import_env()).urls("POST")[0]
        self.assertTrue(real.endswith(described),
                        f"dry run said {described!r} but the real call was {real!r}")

    # --- hygiene ---------------------------------------------------------------

    def test_api_key_is_sent_as_a_header_not_in_the_url(self):
        r = self.run_script(self.plan_stranded_then_healed(), env=self.import_env())
        for call in r.calls:
            self.assertNotIn("KEY", call["url"], "the key must never reach a URL (it gets logged)")
        self.assertTrue(any("X-Emby-Token" in h for c in r.calls for h in c["headers"]))

    def test_api_key_is_never_logged(self):
        r = self.run_script(self.plan_stranded_then_healed(), env=self.import_env())
        self.assertNotIn("KEY", r.output.replace("JELLYFIN_API_KEY", ""))


if __name__ == "__main__":
    unittest.main()
