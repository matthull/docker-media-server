#!/usr/bin/env python3
"""What the disk check actually costs in ntfy messages, driven through the real check.

    python3 monitoring/simulate_disk.py

Runs run_check at the real 15-minute cadence with a fake drive and fake healthy containers, and
counts what publish_stack really sends. Nothing is sent anywhere; the notifier's HTTP is a stub.

It exists because "this won't be noisy" is not something a unit test can tell you. The first version
of the disk check reported the step it was under *right now*, which read fine in tests and was a
noise machine in practice: advance() damps whether a problem is present, but a description is taken
from the latest run undamped, and a changed description republishes the stack notification.

Three scenarios:

  wobble  Free space oscillating across a step. Not hypothetical — SABnzbd pauses at its own
          download_free and fulldisk_autoresume puts it back, and with the 100G default the half
          step is 50G, the number it oscillates around. This is the case that motivated the ratchet.
  fill    A drive filling steadily, which is the case the check is for.
  parked  A drive left below the threshold and never attended to, which is what STACK_REPEAT costs.
          Before the re-nudge this was one message however long it lasted; the question the cadence
          has to answer is what it costs against ntfy.sh's anonymous 250 a day, shared with every
          other service on the host. Measured rather than asserted.

Reported alongside each is what the same run would cost WITHOUT the ratchet, so the difference stays
a number rather than a claim.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stack_watch as sw  # noqa: E402

GB = 1024 ** 3
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
COMPOSE = json.dumps({"name": "proj", "services": {"sonarr": {}}})


def healthy_docker(argv):
    if argv[1] == "compose":
        return COMPOSE
    if argv[1] == "ps":
        return "0\n"
    return json.dumps([{"Config": {"Labels": {"com.docker.compose.service": "sonarr"}},
                        "State": {"Status": "running", "ExitCode": 0}}])


class Stub:
    """Records publishes instead of making them."""

    def __init__(self):
        self.sent = []

    def __call__(self, method, url, headers=None, data=None, timeout=20):
        if method == "POST":
            self.sent.append(json.loads(data))
        return "{}"


def run(free_series, ratchet=True):
    http, state, free = Stub(), {}, None
    original = sw.filesystem_free
    # publish_stack writes "muted: N sent in the last day" to stderr; that is the cap being spent,
    # which the counts below already report, so keep it out of the way.
    with contextlib.redirect_stderr(io.StringIO()):
        return _run(free_series, ratchet, http, state, original)


def _run(free_series, ratchet, http, state, original):
    try:
        for index, free in enumerate(free_series):
            sw.filesystem_free = lambda path, _f=free: (1, _f)
            if not ratchet:
                state.pop("disk", None)  # the floor is what makes it a ratchet
            watch = sw.Watch({"STACK_DIR": "/stack", "JELLYFIN_URL": "off",
                              "HEARTBEAT_GRACE": "off", "MEDIA_ROOT": "/lib",
                              "DISK_FREE_MIN": "100G"},
                             sw.Notifier("https://ntfy.invalid", "t", http),
                             START + timedelta(minutes=15 * index), http, healthy_docker, "h", "h")
            sw.run_check(watch, state)
    finally:
        sw.filesystem_free = original
    return http.sent


def report(label, series, note):
    with_ratchet = run(series, ratchet=True)
    without = run(series, ratchet=False)
    hours = len(series) * 15 / 60
    print(f"=== {label}: {len(series)} checks, {hours:.1f}h")
    print(f"    {note}")
    print(f"    ratcheted (shipped): {len(with_ratchet)} messages, "
          f"{len(without)} without the ratchet, of a {sw.STACK_DAILY_CAP}/day cap")
    for message in with_ratchet:
        print(f"      p{message['priority']} {message['message'].splitlines()[0]}")
    print()


report("wobble across the 50G step",
       [(52 if index % 2 else 48) * GB for index in range(48)],
       "SABnzbd pausing at download_free and auto-resuming looks like this.")

report("steady fill from 200G to empty",
       list(range(200 * GB, 0, -2 * GB)),
       "One message per step crossed, which is what the check is for.")

report("wobble for 12h, then fill to empty",
       [(52 if index % 2 else 48) * GB for index in range(48)] + list(range(48 * GB, 0, -3 * GB)),
       "The case that matters: is the cap still there when the real fall starts?")

WEEK = 7 * 24 * 4  # quarter-hourly checks
report("parked at 5G free for a week",
       [5 * GB] * WEEK,
       f"What the {sw.span(sw.STACK_REPEAT)} re-nudge costs against ntfy.sh's shared 250/day.")
