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
    ("D7", "        except OSError as exc:\n            print(f\"can't read free space for {key}",
     "        except OSError as exc:\n            raise WatchError('x') from exc\n"
     "            print(f\"can't read free space for {key}",
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
    ("R1", "level = floors[key] = min(reached, floors.get(key, reached))",
     "level = floors[key] = max(reached, floors.get(key, reached))",
     "the ratchet runs backwards: the report only ever rises"),
    ("R2", "level = floors[key] = min(reached, floors.get(key, reached))",
     "level = floors[key] = reached",
     "no ratchet, so a wobble across a step alternates and burns the daily cap"),
    ("R3", 'state["disk"] = {key: level for key, level in floors.items() if key in tracked}',
     'state["disk"] = {key: level for key, level in floors.items()}',
     "the floor outlives its problem, so a later dip claims the old low point"),
    ("R4", 'state["disk"] = {key: level for key, level in floors.items() if key in tracked}',
     'state["disk"] = {}',
     "the floor is dropped every run, so the ratchet never spans checks"),
    ("R5", "floors.get(key, reached))", "floors.get(key, 0))",
     "an unseen key floors at zero and reports a level nothing was ever under"),
    ("R6", 'floors = state.setdefault("disk", {})', "floors = {}",
     "run_check hands over a fresh floor each run"),
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
