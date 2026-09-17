#!/bin/sh
# Strip the redundant RefreshMonitoredDownloads write out of Seerr's download tracker.
#
# WHY
#
# Seerr's download tracker POSTs /api/v3/command {"name":"RefreshMonitoredDownloads"}
# to each arr and awaits it *before* it GETs /queue. That POST inserts a row into the
# arr's Commands table, so it needs the arr's SQLite write lock -- which the arr itself
# holds while a grab or an import is in flight. The read never blocks; only the write
# does. Measured on this stack while a season pack was downloading:
#
#   POST /api/v3/command   Radarr 66.35s   Sonarr 31.36s
#   GET  /queue            never above 9ms on either arr, under any condition
#
# When the POST outruns Seerr's client timeout the whole cycle is abandoned, Seerr logs
# "Unable to get queue from <arr> server", and the request card freezes on its last-known
# percentage and ETA instead of updating -- precisely while someone is watching the thing
# they just requested, because the contention is caused by that very download. It only
# reads as fully blank for a download whose first tracker cycles all failed. No timeout
# value fixes this; the tail grows with the download. Not issuing the write does.
#
# The arrs run RefreshMonitoredDownloads on their own schedule regardless, so dropping
# Seerr's copy costs at most about a minute of queue staleness on a progress indicator.
#
# HOW THIS FAILS
#
# Loudly, and on the build host rather than in production. If the two call sites are not
# found exactly as expected -- an upstream refactor, a rename, a bundler change -- this
# script exits non-zero, `docker compose build seerr` fails, no image is produced, and
# the already-built patched image keeps serving -- on a warm host only: on a cold
# `docker compose up -d` a failed build starts no service at all (see README.md). The one
# thing it will never do is quietly hand back an unpatched Seerr.
#
# Runs against busybox sed/grep (the base image is Alpine), so the regex is POSIX BRE.

set -eu

target=/app/dist/lib/downloadtracker.js

die() {
	echo "patch-downloadtracker: ERROR: $1" >&2
	echo "patch-downloadtracker: refusing to build an unpatched Seerr image." >&2
	echo "patch-downloadtracker: see images/seerr/README.md before changing this." >&2
	exit 1
}

[ -f "$target" ] || die "$target does not exist -- upstream layout changed"

refresh_re='^[ 	]*await [A-Za-z0-9_$]*\.refreshMonitoredDownloads();[ 	]*$'
queue_re='^[ 	]*const queueItems = await [A-Za-z0-9_$]*\.getQueue();[ 	]*$'

before=$(grep -c 'refreshMonitoredDownloads' "$target" || true)
[ "$before" -eq 2 ] || die "expected exactly 2 refreshMonitoredDownloads references in $target, found $before"

standalone=$(grep -c "$refresh_re" "$target" || true)
[ "$standalone" -eq 2 ] || die "expected 2 standalone refreshMonitoredDownloads statements, found $standalone"

# Check the PAIR, not just the line. Deleting a statement is only safe if we know what it
# is attached to: each refresh must be immediately followed by the queue read it precedes.
# Without this, a refresh that upstream has moved under a braceless `if` would still pass
# every count check and silently re-bind the following statement into the `if` body.
paired=$(grep -A1 "$refresh_re" "$target" | grep -c "$queue_re" || true)
[ "$paired" -eq 2 ] || die "refresh calls are not both immediately followed by the getQueue they guard ($paired of 2) -- upstream restructured this; read the file before touching the regex"

lines_before=$(wc -l <"$target")

# Deletes a whole line of the form `await <ident>.refreshMonitoredDownloads();`.
# Kept literal rather than interpolating $refresh_re, so the expression sed sees is the one
# written here and not the result of a shell expansion.
sed -i '/^[ 	]*await [A-Za-z0-9_$]*\.refreshMonitoredDownloads();[ 	]*$/d' "$target"

after=$(grep -c 'refreshMonitoredDownloads' "$target" || true)
[ "$after" -eq 0 ] || die "$after refreshMonitoredDownloads reference(s) survived the patch"

lines_after=$(wc -l <"$target")
removed=$((lines_before - lines_after))
[ "$removed" -eq 2 ] || die "expected to remove exactly 2 lines, removed $removed"

node --check "$target" || die "patched $target is not valid JavaScript"

echo "patch-downloadtracker: removed 2 refreshMonitoredDownloads call sites from $target"
