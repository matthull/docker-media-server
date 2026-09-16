#!/usr/bin/env python3
"""Mutation test for jellyfin-refresh-series.sh.

A green suite cannot tell coverage that guards behaviour from coverage that guards nothing.
Each mutation below is a plausible wrong edit; the suite must fail for every one. Runs against a
throwaway copy so the real script is never touched.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "scripts" / "jellyfin-refresh-series.sh"

MUTANTS = [
    ("drop FullRefresh -> Default",
     "metadataRefreshMode=FullRefresh", "metadataRefreshMode=Default"),
    ("refresh even when already healthy (invert the guard)",
     'if [ "$seasons" -gt 0 ]; then\n        log "\'$title\' ($item_id) already has',
     'if [ "$seasons" -lt 0 ]; then\n        log "\'$title\' ($item_id) already has'),
    ("lose the duplicate-worker lock",
     "if ! flock -n 9; then", "if false; then"),
    ("treat a failed season lookup as zero seasons",
     "printf -- '-1 %s' \"$JF_STATUS\"", "printf -- '0 %s' \"$JF_STATUS\""),
    ("accept any HTTP status as success",
     '[ "$JF_STATUS" -ge 200 ] && [ "$JF_STATUS" -lt 300 ]', "true"),
    ("skip the post-refresh confirmation (trust the 204)",
     'if [ "$seasons" -gt 0 ]; then\n            log "\'$title\' ($item_id): HEALED',
     'if true; then\n            log "\'$title\' ($item_id): HEALED'),
    ("do the work in the foreground instead of dispatching",
     'if [ "${REFRESH_CHILD:-0}" != 1 ]; then', "if false; then"),
    ("put the api key in the url instead of a header",
     '-H "X-Emby-Token: $JELLYFIN_API_KEY" \\', "\\"),
    ("ignore the dry-run switch",
     'if [ "$REFRESH_DRYRUN" = 1 ]; then', "if false; then"),
    ("handle only Download, dropping ImportComplete",
     "Download|ImportComplete) ;;", "Download) ;;"),
    ("match series on tvdb only, never on folder",
     'or ($folder != "" and ((.Path // "") | sub("/+$";"") | split("/") | last) == $folder)', ""),
    ("proceed without an api key",
     'if [ -z "$JELLYFIN_API_KEY" ]; then\n        log "JELLYFIN_API_KEY is not set',
     'if false; then\n        log "JELLYFIN_API_KEY is not set'),
    ("run on any event at all",
     '*) log "ignoring event $event"; return 0 ;;', '*) ;;'),
    ("report a non-heal as success",
     'log "\'$title\' ($item_id): STILL STRANDED', 'return 0; log "\'$title\' ($item_id): STILL STRANDED'),
]


def run_suite(script_dir):
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(script_dir)],
        cwd=script_dir.parent, capture_output=True, text=True, timeout=300)


def main():
    original = SRC.read_text()
    work = Path(tempfile.mkdtemp())
    pkg = work / "scripts"
    pkg.mkdir()
    shutil.copy(REPO / "scripts" / "test_jellyfin_refresh.py", pkg)

    shutil.copy(SRC, pkg)
    base = run_suite(pkg)
    if base.returncode != 0:
        print("BASELINE IS NOT GREEN -- fix that first\n", base.stderr[-3000:])
        return 1
    print(f"baseline: green ({original.count(chr(10))} lines)\n")

    killed = survived = 0
    rows = []
    for name, find, repl in MUTANTS:
        if find not in original:
            rows.append(("STALE", name))
            survived += 1
            print(f"STALE    {name}  <- pattern no longer present, mutation never applied")
            continue
        (pkg / SRC.name).write_text(original.replace(find, repl, 1))
        (pkg / SRC.name).chmod(0o755)
        r = run_suite(pkg)
        if r.returncode != 0:
            killed += 1
            rows.append(("killed", name))
            print(f"killed   {name}")
        else:
            survived += 1
            rows.append(("SURVIVED", name))
            print(f"SURVIVED {name}  <- tests did not notice this")

    print(f"\n{killed}/{len(MUTANTS)} killed, {survived} survived")
    return 0 if survived == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
