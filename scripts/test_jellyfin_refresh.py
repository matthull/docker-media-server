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
    sys.stdout.write(body + "\n" + str(rule.get("status", 200)))
    sys.exit(rule.get("exit", 0))

sys.stdout.write("\n404")
'''

STUB_SLEEP = "#!/bin/sh\nexit 0\n"


def series_item(item_id="ITEM1", name="Chewing Gum", tvdb="301562", path="/home/matt/media/complete/tv/Chewing Gum"):
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
                "sonarr_series_title": "Chewing Gum", "sonarr_series_tvdbid": "301562",
                "sonarr_series_path": "/data/complete/tv/Chewing Gum"}
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
             "body": {"Items": [series_item(item_id="OTHER", name="Fleabag", tvdb="314614",
                                            path="/home/matt/media/complete/tv/Fleabag")]}},
        ]
        r = self.run_script(plan, env=self.import_env())
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.urls("POST"), [], "must not refresh an unrelated series")
        self.assertIn("not found in Jellyfin", r.output)

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
