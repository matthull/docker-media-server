#!/usr/bin/env python3
"""Mutation testing for stack_watch.py: does the suite actually guard these lines, or just run them?

    python3 monitoring/mutation_table.py

Each entry below breaks one behaviour on purpose. The suite is run against a throwaway copy of
monitoring/ with that one change applied; a mutant that SURVIVES means every test still passed while
the behaviour was broken, so nothing was guarding it. The repo is never modified.

A green suite cannot tell coverage that guards behaviour from coverage that merely executes it. This
file exists because the disk check's first pass had two survivors that were real: a blank
DISK_FREE_MIN (which is what .env.example ships) silently turning the check off, and "OFF" no longer
meaning off. The test written for the first of those then found a third bug, a whitespace-only value
bypassing the default.

Add an entry whenever you add behaviour worth keeping. The cost is one suite run each (~0.1s).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (id, old source text, replacement, what the mutant breaks)
MUTANTS: list[tuple[str, str, str, str]] = [
    # --- the disk check
    ("D1", "if free >= minimum:", "if free > minimum:",
     "alerts when free space is exactly the threshold"),
    ("D2", "step = min(fraction for fraction", "step = max(fraction for fraction",
     "always reports the top step, so it never escalates"),
    ("D3", "if free < minimum * fraction)", "if free <= minimum * fraction)",
     "off-by-one at every step boundary"),
    ("D4", "filesystems.setdefault(device, ([], free))[0].append(role)",
     "filesystems.setdefault(role, ([], free))[0].append(role)",
     "one entry per role instead of per filesystem, so a single drive alerts twice"),
    ("D5", "        if not path:\n            continue\n", "        if False:\n            continue\n",
     "a path that isn't configured is read anyway"),
    ("D6", 'problems[f"disk:{role}"] = f"disk: can\'t read free space for the {role} path"',
     'problems[f"disk:{role}"] = f"disk: can\'t read free space: {exc}"',
     "leaks the OS error text to the public topic"),
    ("D7", "        except OSError as exc:\n            if not configured:",
     "        except OSError as exc:\n            raise WatchError('x') from exc\n"
     "            if not configured:",
     "one unreadable path kills the container and Jellyfin checks too"),
    ("D8", 'if minimum < SIZE_UNITS["G"]:', "if minimum < 0:",
     "accepts 0G, which silently disables the check"),
    ("D9", 'raise WatchError("DISK_FREE_MIN must be a size like 100G, or off") from exc',
     "return SIZE_UNITS['G'] * 100",
     "a typo'd setting falls back silently instead of reporting itself"),
    ("D10", 'text = (cfg.get("DISK_FREE_MIN") or "").strip() or DISK_FREE_MIN_DEFAULT',
     'text = (cfg.get("DISK_FREE_MIN") or "").strip() or "off"',
     "a blank setting turns the check off instead of using the default"),
    ("D10b", 'text = (cfg.get("DISK_FREE_MIN") or "").strip() or DISK_FREE_MIN_DEFAULT',
     'text = (cfg.get("DISK_FREE_MIN") or DISK_FREE_MIN_DEFAULT).strip()',
     "a whitespace-only setting is parsed instead of defaulting"),
    ("D11", 'if text.lower() == "off":', 'if text == "off":', '"OFF" and "Off" stop meaning off'),
    ("D11b", 'if text.lower() == "off":', 'if text.lower() == "OFF":', '"off" stops meaning off'),
    ("D12", "        problems |= disk_problems(watch.cfg, minimum, floors if floors is not None else {})",
     "        disk_problems(watch.cfg, minimum, floors if floors is not None else {})",
     "the disk check runs but its result is thrown away"),
    ("D13", "    if minimum is not None:", "    if minimum is None:",
     "the check is inverted: on when off, off when on"),
    ("D14", 'if minimum < SIZE_UNITS["G"]:', 'if minimum < SIZE_UNITS["M"]:',
     "accepts a threshold of a few megabytes"),
    # --- the Jellyfin check, whose error text is the one that quotes JELLYFIN_URL back
    ("J1", 'return "jellyfin: not answering (details in the journal)"',
     'return f"jellyfin: not answering ({brief(getattr(exc, \'reason\', exc))})"',
     "leaks the OS/TLS error text, and with it the host JELLYFIN_URL names, to the public topic"),
    ("J2", '        print(f"jellyfin health check failed: {exc}", file=sys.stderr)\n',
     "",
     "drops the real reason entirely, so the journal can't say why Jellyfin was unreachable"),
    # --- the ratchet, which is what keeps a wobbling drive from spending the day's alerts
    ("R1", "level = min([int(minimum * step)] + kept)", "level = max([int(minimum * step)] + kept)",
     "the ratchet runs backwards: the report only ever rises"),
    ("R2", "level = min([int(minimum * step)] + kept)", "level = int(minimum * step)",
     "no ratchet, so a wobble across a step alternates and burns the daily cap"),
    ("R3", 'state["disk"] = {key: level for key, level in floors.items() if key in tracked}',
     'state["disk"] = {key: level for key, level in floors.items()}',
     "the floor outlives its problem, so a later dip claims the old low point"),
    ("R4", 'state["disk"] = {key: level for key, level in floors.items() if key in tracked}',
     'state["disk"] = {}',
     "the floor is dropped every run, so the ratchet never spans checks"),
    ("R5", '        kept = [floor["level"] for key in keys\n'
           '                if (floor := floors.get(key)) and floor.get("device") in (device, None)]',
     '        kept = [floors.get(key, {}).get("level", 0) for key in keys]',
     "an unseen key floors at zero and reports a level nothing was ever under"),
    ("R6", 'floors = state.setdefault("disk", {})', "floors = {}",
     "run_check hands over a fresh floor each run"),
    # --- keying per role, so splitting or merging the watched paths doesn't retire a key
    ("K1", 'keys = [f"disk:{role}" for role in roles]', 'keys = [f"disk:{\'+\'.join(roles)}"]',
     "back to a role-set key, so moving downloads to its own drive invents a new problem"),
    ("K2", '        for key in keys:\n            floors[key] = {"device": device, "level": level}',
     '        for key in keys[:1]:\n            floors[key] = {"device": device, "level": level}',
     "only the first role of a filesystem keeps a floor"),
    # --- a floor belongs to a filesystem, not to a role that may move to another drive
    ("V1", '(floor := floors.get(key)) and floor.get("device") in (device, None)',
     "(floor := floors.get(key))",
     "a floor follows a role onto a new drive and claims a level it was never under"),
    ("V2", '(floor := floors.get(key)) and floor.get("device") in (device, None)',
     '(floor := floors.get(key)) and floor.get("device") == device',
     "an inherited floor with no device yet is thrown away, losing the reported low point"),
    ("V3", 'floors[key] = {"device": device, "level": level}',
     'floors[key] = {"device": None, "level": level}',
     "the device is never recorded, so a floor can never be recognised as foreign"),
    # --- migrating the old disk:library+downloads key
    ("G1", "    migrate_disk_keys(state)\n", "\n",
     "upgrading retires the key in front of the artifex and invents two, at top priority"),
    ("G2", '            for role in key.removeprefix("disk:").split("+"):\n'
           "                holder.setdefault(f\"disk:{role}\", value)",
     '            for role in key.removeprefix("disk:").split("+")[:1]:\n'
     "                holder.setdefault(f\"disk:{role}\", value)",
     "only the first role of an old combined key is migrated"),
    ("G3", "    for holder in (state.get(\"disk\"), state.get(\"tracked\"),\n"
           "                   state.get(\"stack\", {}).get(\"shown\"), state.get(\"stack\", {}).get(\"reported\")):",
     "    for holder in (state.get(\"disk\"), state.get(\"tracked\")):",
     "shown isn't migrated, so the same key churn happens one layer down"),
    ("G4", '            floors[key] = {"level": level}  # no device', '            floors[key] = {}  # no device',
     "an inherited floor loses its level, so the ratchet restarts at the top step"),
    # --- the abandoned read must not be able to hold up the interpreter's exit
    ("T4", "    worker = threading.Thread(target=attempt, daemon=True)",
     "    worker = threading.Thread(target=attempt, daemon=False)",
     "a wedged read is joined at shutdown, which is the SIGTERM the timeout exists to prevent"),
    ("T5", "DISK_READ_TIMEOUT = 20", "DISK_READ_TIMEOUT = 2000",
     "33 minutes for one path, well past the unit's TimeoutStartSec"),
    # --- a filesystem that can never satisfy the threshold, which Compose's fallback usually is
    ("F5", "        if not configured and total < minimum:", "        if False:",
     "a few-gigabyte tmpfs alerts forever on the same notification as the real library-full alert"),
    ("F6", "        if not configured and total < minimum:", "        if total < minimum:",
     "a path the user configured is second-guessed and silently not watched"),
    ("F4", '("MEDIA_ROOT", "library", None)', '("MEDIA_ROOT", "library", "/tmp")',
     "MEDIA_ROOT gains a fallback Compose doesn't apply, so an unset one watches the wrong drive"),
    # --- the re-send's cadence and its one chance to say something false
    ("N6", "STACK_REPEAT = timedelta(days=1)", "STACK_REPEAT = timedelta(days=7)",
     "the cadence is retuned to a week and the re-send still fires daily"),
    # N7 pins what makes STACK_REPEAT real. It is the pruning window, not a comparison: the clause
    # that looked like the cadence was dead code, and this mutant surviving is what proved it.
    ("N7", "                  if now - t < max(day, STACK_REPEAT))", "                  if now - t < day)",
     "the re-send window is pinned at a day, so raising STACK_REPEAT changes nothing"),
    ("N8", "min([t for t in started if t], default=None)",
     "max([t for t in started if t], default=None)",
     "'First seen' names the newest problem in the set instead of the oldest"),
    ("N9", "current and not going and not sent", "current and not sent",
     "a problem that has already gone is re-sent as 'still not resolved'"),
    # --- the fallback Compose applies when SABNZBD_TEMP is blank
    ("F1", "path = configured or fallback", "path = configured",
     "a blank SABNZBD_TEMP watches nothing, while Compose still mounts its default"),
    ("F2", "            if not configured:\n                # Compose's fallback",
     "            if False:\n                # Compose's fallback",
     "an absent fallback path is alerted on as if the user had asked for it"),
    ("F3", '("SABNZBD_TEMP", "downloads", "/tmp/sabnzbd-temp")',
     '("SABNZBD_TEMP", "downloads", "/tmp/sabnzbd")',
     "the fallback drifts from the one docker-compose.yml applies"),
    # --- reading the disk without a timeout of its own
    ("T1", '    if "value" not in outcome:', "    if False:",
     "a wedged filesystem is not given up on, so the whole run is SIGTERMed"),
    ("T2", '    if "error" in outcome:', "    if False:",
     "a real error is reported as a timeout instead of itself"),
    ("T3", "        except OSError as exc:\n            if not configured:",
     "        except FileNotFoundError as exc:\n            if not configured:",
     "a timed-out read escapes instead of reading as an unreadable path"),
    # --- re-sending a standing problem, so it isn't announced once and then never again
    ("N1", "    if unchanged and not (current and not going and not sent):", "    if unchanged:",
     "a problem is announced exactly once, ever, however long it lasts"),
    ("N3", "    fresh = bool(current.keys() - shown.keys() - reported.keys())",
     "    fresh = bool(current)",
     "a flapping problem's return overrides the cap that protects the shared ntfy budget"),
    ("N4", "    if repeat:\n        message += \"\\n\\nStill not resolved.\"",
     "    if False:\n        message += \"\\n\\nStill not resolved.\"",
     "a re-sent notification doesn't say so, so it reads as fresh news"),
    ("N4b", '        message += "\\n\\nStill not resolved."',
     '        message += f"\\n\\nUnchanged."',
     "a repeat claims nothing changed, to someone who may have just freed 60G"),
    ("N5", "        current, watch.host, worsened, [d for k, d in shown.items() if k not in current],\n"
           "        unchanged, min([t for t in started if t], default=None))",
     "        current, watch.host, worsened, [d for k, d in shown.items() if k not in current],\n"
     "        True, min([t for t in started if t], default=None))",
     "'Still not resolved' is stamped on genuinely new and worsening alerts too"),
    ("U1", '            "sent", [watch.now.isoformat()] if state["stack"].get("shown") else [])',
     '            "sent", [])',
     "upgrading from state without a stack record re-sends every alert it had already sent"),
    ("U2", '            "sent", [watch.now.isoformat()] if state["stack"].get("shown") else [])',
     '            "sent", [watch.now.isoformat()])',
     "a first install spends a slot of the daily cap on a notification that never went out"),
    # --- what the notification actually says
    ("C1", "        if cleared:  # distinct", "        if False:  # distinct",
     "the all clear never names what recovered, which is a ratcheted disk's only acknowledgement"),
    ("C1b", 'for d in sorted(set(cleared))', "for d in sorted(cleared)",
     "one drive shared by both roles is listed twice in the all clear"),
    ("C2", "lines = sorted(set(current.values()))", "lines = sorted(current.values())",
     "a single drive shared by both roles reports the same problem twice"),
    ("C3", '                        tags=("white_check_mark",) if not current\n'
           '                        else ("rotating_light",) if worsened else ("warning",))',
     '                        tags=("rotating_light",) if worsened else ("white_check_mark",))',
     "a notification still titled 'N problems' carries a check mark"),
    # --- sizes
    ("S1", r'r"\s*(\d+(?:\.\d+)?)\s*([MGT])\s*"', r'r"\s*(\d+(?:\.\d+)?)\s*([MGTK])\s*"',
     "accepts K, which SIZE_UNITS has no entry for"),
    ("S2", "return int(float(match[1]) * SIZE_UNITS[match[2].upper()])",
     "return int(float(match[1]) * SIZE_UNITS[match[2].upper()]) // 1024",
     "every size is a thousandfold too small"),
    ("S3", '(("T", SIZE_UNITS["T"]), ("G", SIZE_UNITS["G"]), ("M", SIZE_UNITS["M"]))',
     '(("M", SIZE_UNITS["M"]), ("G", SIZE_UNITS["G"]), ("T", SIZE_UNITS["T"]))',
     "renders 100G as 102400M"),
    ("S4", 'return f"{nbytes / scale:.10g}{unit}"', 'return f"{nbytes / scale:.1g}{unit}"',
     "rounds 102.4T to 100T and 1.5G to 2G"),
    # --- the stack notification's priority, which the ratchet made load-bearing
    ("P1", "    worsened = bool(current.keys() - shown.keys()) or any(\n"
           "        current[key] != shown[key] for key in current.keys() & shown.keys())",
     "    worsened = bool(current.keys() - shown.keys())",
     "a worsening step goes out at Default with a white_check_mark"),
]


def suite_passes(where: Path) -> bool:
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "monitoring"],
                          cwd=where, capture_output=True, text=True).returncode == 0


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "tree"
        shutil.copytree(REPO / "monitoring", tree / "monitoring")
        # The suite asserts against files outside monitoring/ too — DISK_ROLES' fallback has to stay
        # the one Compose applies — so they have to exist in the throwaway tree as well.
        shutil.copy(REPO / "docker-compose.yml", tree / "docker-compose.yml")
        if not suite_passes(tree):
            print("The suite is already failing; fix that before reading anything into mutants.")
            return 1
        target = tree / "monitoring" / "stack_watch.py"
        original = target.read_text()
        killed, survived, missing = [], [], []
        for ident, old, new, breaks in MUTANTS:
            if old not in original:
                missing.append(ident)
                print(f"  {ident:5} STALE     — source no longer contains this; update the entry")
                continue
            target.write_text(original.replace(old, new, 1))
            alive = suite_passes(tree)
            target.write_text(original)
            (survived if alive else killed).append(ident)
            print(f"  {ident:5} {'SURVIVED ' if alive else 'killed   '} — {breaks}")
        print(f"\n{len(killed)} killed, {len(survived)} survived, {len(missing)} stale, "
              f"of {len(MUTANTS)}")
        if survived:
            print("A survivor is a behaviour no test is guarding. Write the test, don't delete the "
                  "mutant.")
        return 1 if survived or missing else 0


if __name__ == "__main__":
    sys.exit(main())
