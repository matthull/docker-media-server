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

Add an entry whenever you add behaviour worth keeping. The cost is one suite run each (~2s; B2, which
never gives up on a hung call, waits out every hung fake and takes about two minutes).
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
    ("D7", '        except OSError as exc:\n            if not configured and f"disk:{role}" not in floors:',
     "        except OSError as exc:\n            raise WatchError('x') from exc\n"
     '            if not configured and f"disk:{role}" not in floors:',
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
    ("D12", "        problems |= disk_problems(watch.cfg, minimum, floors if floors is not None else {},\n"
            "                                  free_now=free_now)",
     "        disk_problems(watch.cfg, minimum, floors if floors is not None else {},\n"
     "                      free_now=free_now)",
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
    # --- Seerr's download-tracker patch, which its own healthcheck cannot see
    ("SP1", '    if "refreshMonitoredDownloads" in source:', "    if False:",
     "an unpatched Seerr is reported as patched"),
    ("SP2", '    if "getQueue" not in source:', "    if False:",
     "an empty or unrelated file passes for a patched tracker, because the patch is an absence"),
    ("SP3", '        return "seerr: can\'t check its download-tracker patch (details in the journal)"',
     '        return f"seerr: can\'t check its download-tracker patch ({brief(exc)})"',
     "leaks docker's error text to the public topic"),
    ("SP4", '        print(f"seerr patch check failed: {exc}", file=sys.stderr)\n', "",
     "drops the real reason, so the journal can't say why the check failed"),
    ("SP5", "    except (*COMMAND_ERRORS, UnicodeDecodeError) as exc:  # run_cmd decodes",
     "    except (RuntimeError, UnicodeDecodeError) as exc:  # run_cmd decodes",
     "a hung exec times out through the whole run instead of being one problem"),
    ("SP14", "    except (*COMMAND_ERRORS, UnicodeDecodeError) as exc:  # run_cmd decodes",
     "    except COMMAND_ERRORS as exc:  # run_cmd decodes",
     "an undecodable tracker file fails the whole run, taking every other alert with it"),
    ("SP15", 'if SEERR_SERVICE in config["services"] and f"service:{SEERR_SERVICE}" not in problems:',
     'if SEERR_SERVICE in config["services"] and not problems:',
     "any unrelated stopped service skips the check, and an unpatched Seerr reads as Cleared"),
    ("SP16", '        elif f"service:{SEERR_SERVICE}" in problems and SEERR_PATCH in previous:',
     '        elif f"service:{SEERR_SERVICE}" in problems:',
     "a stopped Seerr with no patch history raises KeyError and fails the whole run"),
    ("SP6", "], timeout=SEERR_EXEC_TIMEOUT)", "])",
     "the read waits run_cmd's 60s, 55s of the run's time budget the docs don't account for"),
    ("SP7", "SEERR_EXEC_TIMEOUT = 5", "SEERR_EXEC_TIMEOUT = 60",
     "the same, by retuning the constant"),
    ("SP8", 'if SEERR_SERVICE in config["services"] and f"service:{SEERR_SERVICE}" not in problems:',
     'if f"service:{SEERR_SERVICE}" not in problems:',
     "a stack without Seerr reports 'can't check' on every run"),
    ("SP9", 'if SEERR_SERVICE in config["services"] and f"service:{SEERR_SERVICE}" not in problems:',
     'if SEERR_SERVICE in config["services"]:',
     "a stopped Seerr is reported twice, once as stopped and once as unreadable"),
    ("SP10", "        if problem:\n            problems[SEERR_PATCH] = problem", "        if False:\n            pass",
     "the patch check runs but its result is thrown away"),
    ("SP11", 'if k.startswith("service:") or k == SEERR_PATCH}', 'if k.startswith("service:")}',
     "Docker going down announces the patch as Cleared, unchecked, then re-alerts"),
    ("SP12", '        elif f"service:{SEERR_SERVICE}" in problems and SEERR_PATCH in previous:',
     "        elif False:",
     "stopping Seerr announces the patch as Cleared, unchecked"),
    ("SP17", "problem = seerr_patch_problem(run, running[0])",
     "problem = seerr_patch_problem(run, SEERR_SERVICE)",
     "the check reads whatever container answers to the name, not the one judged running"),
    ("SP18", 'if is_up(c["State"])]\n', "]\n",
     "an exited or unhealthy Seerr container beside the live one can be the one read"),
    ("SP19", '        if labels.get("com.docker.compose.oneoff") != "False":\n            continue\n', "",
     "a one-off or hand-run seerr counts as the service, and can be the container read"),
    ("SP23", 'if labels.get("com.docker.compose.oneoff") != "False":',
     'if labels.get("com.docker.compose.oneoff") == "True":',
     "`docker run` of the built image counts as the service: it can mask a missing Seerr, and be the "
     "container the patch check reads"),
    ("SP20", '            patch["description"] = SEERR_UNPATCHED\n', "            pass\n",
     "a timed-out read during a standing unpatched alert sends two extra High notifications"),
    ("SP21", 'if patch and (state["stack"].get("shown") or {}).get(SEERR_PATCH) == SEERR_UNPATCHED:',
     "if patch:",
     "a fresh failed read is reworded as unpatched on the strength of nothing"),
    ("SP22", '(state["stack"].get("shown") or {}).get(SEERR_PATCH) == SEERR_UNPATCHED',
     '(state["stack"].get("shown") or {}).get(SEERR_PATCH) is not None',
     "a Seerr only ever unreadable is promoted to running unpatched"),
    ("SP24", '(state["stack"].get("shown") or {}).get(SEERR_PATCH) == SEERR_UNPATCHED',
     'patch["count"] > 1',
     "the pre-fix condition: the guard covers the run that first publishes, so the first "
     "notification reads as a confirmed unpatched Seerr when that run's read in fact failed"),
    ("SP25", '(state["stack"].get("shown") or {}).get(SEERR_PATCH) == SEERR_UNPATCHED',
     '(previous.get(SEERR_PATCH) or {}).get("alerted")'
     ' and (previous.get(SEERR_PATCH) or {}).get("description") == SEERR_UNPATCHED',
     "pinned by what was last tracked rather than last published: `alerted` is set before "
     "publish_stack runs, so an alert ntfy refused still silences the failed read after it"),
    ("SP28", 'state.setdefault("stack", {"shown": inherited})', 'state.setdefault("stack", {"shown": {}})',
     "a state file older than the stack record forgets what it alerted, so the first run after an "
     "upgrade rewords a standing unpatched alert"),
    # SP26 and SP27 are the table's two equivalent mutants, kept knowingly. Nothing observable
    # changes: the only write through the alias sets a field to the value it already holds. They are
    # killed by an assertIsNot, so they pin the structure — no entry shared between `tracked` and
    # the state `previous` still points at — rather than an output. Read them as "this stays a copy",
    # and if a future edit ever writes a differing value here, that is when they start breaking a
    # behaviour. Everything else in this file earns its place by changing what goes out.
    ("SP26", "tracked[SEERR_PATCH] = dict(previous[SEERR_PATCH])",
     "tracked[SEERR_PATCH] = previous[SEERR_PATCH]",
     "a stopped Seerr carries the entry forward by reference, so a later write through it would edit "
     "state the run has already read (equivalent today; pins the copy)"),
    ("SP27", 'tracked |= {k: dict(v) for k, v in previous.items()', 'tracked |= {k: v for k, v in previous.items()',
     "Docker being down carries entries forward by reference, same trap (equivalent today; pins the "
     "copy)"),
    ("SP13", 'SEERR_TRACKER = "/app/dist/lib/downloadtracker.js"',
     'SEERR_TRACKER = "/app/dist/lib/downloadTracker.js"',
     "the check reads a path the build never edits"),
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
     "a wedged read or request is joined at shutdown, which is the SIGTERM the timeout exists to prevent"),
    # --- every timeout the run's budget adds up has to be a ceiling, and the sum has to fit
    ("B1", '    return give_up_after(timeout, "the request", fetch)', "    return fetch()",
     "urlopen's per-operation timeout is trusted, so a trickling reply runs on past it"),
    ("B2", "    worker.join(seconds)", "    worker.join()",
     "nothing is ever given up on, so a hung read or request holds the run until systemd kills it"),
    ("B3", '    return give_up_after(timeout, "the request", fetch)',
     "    return give_up_after(timeout, url, fetch)",
     "a request given up on quotes its URL, and the ntfy URL is the topic"),
    ("B4", "def run_cmd(argv: list, timeout: float = 60) -> str:",
     "def run_cmd(argv: list, timeout: float = 150) -> str:",
     "a check can outlast the unit's TimeoutStartSec, and is killed with nothing saved"),
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
    ("F2", '            if not configured and f"disk:{role}" not in floors:\n'
           "                # Compose's fallback",
     "            if False:\n                # Compose's fallback",
     "an absent fallback path is alerted on as if the user had asked for it"),
    # --- ...but only while it isn't already a problem, or its silence reads as recovery
    ("F7", '            if not configured and f"disk:{role}" not in floors:',
     "            if not configured:",
     "a vanished fallback decays into an all clear that names it as 'Cleared'"),
    ("F8", '            if not configured and f"disk:{role}" not in floors:',
     '            if f"disk:{role}" not in floors:',
     "a configured path that has never been low is skipped instead of reported unreadable"),
    ("F3", '("SABNZBD_TEMP", "downloads", "/tmp/sabnzbd-temp")',
     '("SABNZBD_TEMP", "downloads", "/tmp/sabnzbd")',
     "the fallback drifts from the one docker-compose.yml applies"),
    # --- reading the disk without a timeout of its own
    ("T1", '    if "value" not in outcome:', "    if False:",
     "a wedged filesystem is not given up on, so the whole run is SIGTERMed"),
    ("T2", '    if "error" in outcome:', "    if False:",
     "a real error is reported as a timeout instead of itself"),
    ("T3", '        except OSError as exc:\n            if not configured and f"disk:{role}" not in floors:',
     '        except FileNotFoundError as exc:\n            if not configured and f"disk:{role}" not in floors:',
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
    ("N5", "        unchanged, min([t for t in started if t], default=None), free_now)",
     "        True, min([t for t in started if t], default=None), free_now)",
     "'Still not resolved' is stamped on genuinely new and worsening alerts too"),
    ("U1", '            "sent", [watch.now.isoformat()] if state["stack"].get("shown") else [])',
     '            "sent", [])',
     "upgrading from state without a stack record re-sends every alert it had already sent"),
    ("U2", '            "sent", [watch.now.isoformat()] if state["stack"].get("shown") else [])',
     '            "sent", [watch.now.isoformat()])',
     "a first install spends a slot of the daily cap on a notification that never went out"),
    # --- what the notification actually says
    ("C1", '                "Everything that was down is back up." + cleared_lines(cleared), LOW)',
     '                "Everything that was down is back up.", LOW)',
     "the all clear never names what recovered, which is a ratcheted disk's only acknowledgement"),
    ("C1b", "    gone = sorted(set(cleared))", "    gone = sorted(cleared)",
     "one drive shared by both roles is listed twice among what recovered"),
    ("C2", '    lines = sorted({description + (f" (now {size(free_now[key], MEASURED)} free)"\n'
           '                                   if key in free_now else "")\n'
           "                    for key, description in current.items()})",
     '    lines = sorted([description + (f" (now {size(free_now[key], MEASURED)} free)"\n'
     '                                   if key in free_now else "")\n'
     "                    for key, description in current.items()])",
     "a single drive shared by both roles reports the same problem twice"),
    # --- a recovery while other problems remain, which used to be dropped without a word
    ("C4", '    message = "\\n".join(f"• {line}" for line in lines) + cleared_lines(cleared)',
     '    message = "\\n".join(f"• {line}" for line in lines)',
     "a problem recovering is erased unless it was the last one, so a partial recovery goes unsaid"),
    ("C5", "    return \"\\n\\nCleared:\\n\" + \"\\n\".join(f\"• {d}\" for d in gone) if gone else \"\"",
     '    return "\\n\\nCleared:\\n" + "\\n".join(f"• {d}" for d in gone)',
     "an empty 'Cleared:' header is stamped on every notification that cleared nothing"),
    # --- the current free figure, carried beside the ratcheted sentence rather than inside it
    ("W1", "            if free_now is not None:\n                free_now[key] = free",
     "            if False:\n                free_now[key] = free",
     "a settled sentence re-sent a day later still reads as a claim about right now"),
    ("W2", "            if free_now is not None:\n                free_now[key] = free",
     "            if free_now is not None:\n                free_now[key] = level",
     "the 'now' figure is the ratcheted low point again, so it can never disagree with the sentence"),
    ("W3", "    free_now: dict[str, int] = {}", '    free_now: dict = state.setdefault("free_now", {})',
     "a figure read on an earlier run is carried over and quoted as this run's"),
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
    ("S4", 'def size(nbytes: float, precision: str = ".10g") -> str:',
     'def size(nbytes: float, precision: str = ".1g") -> str:',
     "rounds 102.4T to 100T and 1.5G to 2G"),
    ("S5", 'MEASURED = ".4g"', 'MEASURED = ".10g"',
     "a measured free-space figure goes out as 142.5914421G"),
    ("S6", 'MEASURED = ".4g"', 'MEASURED = ".3g"',
     "1023.9G free renders as the exponent 1.02e+03G"),
    # --- the stack notification's priority, which the ratchet made load-bearing
    ("P1", "    worsened = bool(current.keys() - shown.keys()) or any(\n"
           "        current[key] != shown[key] for key in current.keys() & shown.keys())",
     "    worsened = bool(current.keys() - shown.keys())",
     "a worsening step goes out at Default with a white_check_mark"),
    # --- cancelling the dead man's switch, where ntfy answers 200 to a cancel it ignored
    ("X1", "        armed = watch.notifier.pending(sequence_id)\n        if armed is not True:\n"
           "            break",
     "        armed = watch.notifier.pending(sequence_id)\n        break",
     "one cancel is assumed to have worked, which is the bug: ntfy answers 200 either way"),
    # No X2: dropping `event == "message"` from pending()'s filter is an equivalent mutant, not a
    # gap. A cancel's marker was measured to appear in BOTH feeds, so it is in `waiting` and in
    # `delivered` alike and subtracts out of the difference whatever the filter says. The filter is
    # kept for what it says about intent, and because it is the only thing that would still hold if
    # ntfy ever returned a marker in one feed and not the other — behaviour never observed here, and
    # not worth a test written to match a guess about a server.
    ("X3", '                    if event.get("sequence_id") == sequence_id and event.get("event") == "message"}',
     '                    if event.get("event") == "message"}',
     "another host's scheduled alert on the same topic reads as this one's"),
    ("X13", "        return bool(messages(waiting) - messages(delivered))",
     "        return bool(messages(waiting))",
     "every heartbeat the host ever delivered reads as still armed, so no cancel is ever believed"),
    ("X14", '        waiting = self.feed("poll=1&scheduled=1&since=all")',
     '        waiting = self.feed("poll=1&since=all")',
     "the feed that knows about waiting messages is never asked, so nothing is ever armed"),
    ("X4", "    if 0 < pause <= settle:\n        sleep(pause)", "    if False:\n        sleep(pause)",
     "the cancel goes out inside the second ntfy ignores, wasting the first attempt"),
    ("X15", "    except (AttributeError, TypeError, ValueError):", "    except ZeroDivisionError:",
     "a hand-edited timestamp in the state file stops the disarm from cancelling at all"),
    ("X5", "    if armed:\n", "    if False:\n",
     "an alert that is still armed after every attempt is reported as a clean disarm"),
    ("X6", "    if armed is None:\n", "    if False:\n",
     "a cancel ntfy could not confirm is reported as confirmed"),
    ("X7", "    if dry_run:\n        watch.notifier.delete(sequence_id)\n        return 0",
     "    if False:\n        watch.notifier.delete(sequence_id)\n        return 0",
     "--dry-run polls the real topic and waits"),
    ("X8", "        beat[\"published\"] = now.isoformat()", "        pass",
     "disarm can't tell a heartbeat published a second ago from one published an hour ago"),
    # --- the window in which nobody can say whether ntfy already released the message
    ("X9", "    if due is not None and now > due - CANCEL_SETTLE:",
     "    if due is not None and now > due:",
     "a run racing ntfy's sender strands an unreachable alert with no all clear ever coming"),
    ("X10", 'went_out = (f"The unreachable alert went out {when(due)}." if now > due else',
     'went_out = (f"The unreachable alert went out {when(due)}." if True else',
     "a recovery asserts a delivery that may not have happened"),
    ("X11", "CANCEL_SETTLE = timedelta(seconds=5)", "CANCEL_SETTLE = timedelta(seconds=0)",
     "the blind spot is treated as instantaneous, so neither the wait nor the race window exists"),
    ("X12", "CANCEL_ATTEMPTS = 3", "CANCEL_ATTEMPTS = 1", "a cancel ntfy ignored is never retried"),
    # --- scheduling that has to stay inside ntfy's delay limit
    ("GR1", "    longest = MAX_DELAY - timing.heartbeat_step - DELAY_MARGIN",
     "    longest = MAX_DELAY - timing.heartbeat_step",
     "the longest grace schedules exactly at ntfy's limit, where clock skew 400s the publish"),
    # --- a disk that fills after the probe proved the state file writable
    ("SV1", "        try:\n            save_json(state_path, state)\n        except OSError as exc:",
     "        try:\n            save_json(state_path, state)\n        except ZeroDivisionError as exc:",
     "a disk filling mid-run leaves through an uncaught OSError with nothing naming the state file"),
    ("SV2", '                  f"and may repeat: {exc}", file=sys.stderr)\n            return 1',
     '                  f"and may repeat: {exc}", file=sys.stderr)\n            return code',
     "systemd is told the run succeeded although nothing it decided was recorded"),
    # --- a reply cut short, which is not an OSError
    ("H1", "    except (OSError, HTTPException) as exc:  # URLError and timeouts; a truncated reply",
     "    except OSError as exc:  # URLError and timeouts; a truncated reply",
     "a restarting Jellyfin is reported as the watch itself failing, and takes the run down"),
    ("H2", "        except (OSError, ValueError, HTTPException) as exc:",
     "        except (OSError, ValueError) as exc:",
     "a restarting arr is reported as the watch itself failing"),
    # --- an arr that isn't part of this stack
    ("A1", 'return (cfg.get(f"{name.upper()}_URL") or "").strip().lower() == "off"',
     "return False",
     "a stack without one of the arrs gets a daily failure with no way to switch it off"),
    ("A2", 'return (cfg.get(f"{name.upper()}_URL") or "").strip().lower() == "off"',
     'return (cfg.get(f"{name.upper()}_URL") or "").strip() == "off"',
     '"OFF" and "Off" stop meaning off, unlike every other setting here'),
    ("A3", 'return (cfg.get(f"{name.upper()}_URL") or "").strip().lower() == "off"',
     "return True", "the digest silently checks nothing and reports no stalled titles, ever"),
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
        # Likewise the Seerr check's path has to stay the one the image build patches.
        (tree / "images" / "seerr").mkdir(parents=True)
        shutil.copy(REPO / "images" / "seerr" / "patch-downloadtracker.sh",
                    tree / "images" / "seerr" / "patch-downloadtracker.sh")
        # And the run-time figures the docs quote have to be the ones the suite derives.
        (tree / "docs").mkdir()
        shutil.copy(REPO / "docs" / "stack-watch.md", tree / "docs" / "stack-watch.md")
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
