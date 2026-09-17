#!/bin/bash
# Installs the stack watch (stack_watch.py) as two systemd *user* timers:
#
#   media-stack-watch     `check` every 15 minutes: containers, Jellyfin, Docker, free space, the
#                         Seerr download-tracker patch, and the "host is unreachable" dead man's
#                         switch
#   media-stack-stalled   `stalled` daily: wanted titles that never download
#
# Run as the user who runs Docker, without sudo:
#     ./monitoring/install-stack-watch.sh               install, or update in place
#     ./monitoring/install-stack-watch.sh --uninstall   remove, and cancel the pending alert
#
# The units are generated rather than shipped as static files so the absolute path to the script
# is always correct for wherever this repo is checked out. Each service is started once by hand,
# and must succeed, before any timer is enabled: `stalled` first, then `check`, which schedules the
# real dead man's switch. On a first install that fails, the switch is cancelled again, or it would
# send "unreachable" with no timer left to keep moving it.
#
# User timers only run while the user's systemd instance does. Without linger that means only
# while you are logged in, so the installer warns when linger is off.
#
# Persistent=true matters on a machine that sleeps: a check or digest that was due while it slept
# runs shortly after it wakes, which is also what sends the "back online" update.

set -euo pipefail

[ "$(id -u)" -ne 0 ] || { echo "Run this as your own user, without sudo." >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WATCH="$SCRIPT_DIR/stack_watch.py"
STACK_DIR="$(dirname "$SCRIPT_DIR")"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CHECK=media-stack-watch
STALLED=media-stack-stalled

[ -f "$WATCH" ] || { echo "Not found: $WATCH" >&2; exit 1; }
PYTHON="$(command -v python3)" || { echo "python3 is not installed." >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now "$CHECK.timer" "$STALLED.timer" 2>/dev/null || true
    # A check still running could schedule the alert again right after disarm.
    systemctl --user stop "$CHECK.service" "$STALLED.service" 2>/dev/null || true
    # Without this, the last scheduled "unreachable" alert still fires when its grace runs out.
    "$PYTHON" "$WATCH" disarm || echo "Could not cancel the pending unreachable alert (see above)." >&2
    rm -f "$UNIT_DIR/$CHECK.service" "$UNIT_DIR/$CHECK.timer" \
          "$UNIT_DIR/$STALLED.service" "$UNIT_DIR/$STALLED.timer"
    systemctl --user daemon-reload
    echo "Uninstalled. State is kept in ${XDG_STATE_HOME:-$HOME/.local/state}/media-stack-watch."
    exit 0
fi
[ $# -eq 0 ] || { echo "Usage: $0 [--uninstall]" >&2; exit 2; }

DOCKER="$(command -v docker)" || { echo "docker is not on PATH." >&2; exit 1; }
grep -Eq '^NTFY_TOPIC=.+' "$STACK_DIR/.env" 2>/dev/null ||
    { echo "Set NTFY_TOPIC in $STACK_DIR/.env first; see .env.example." >&2; exit 1; }

mkdir -p "$UNIT_DIR"

write_service() {  # name, description, command
    cat > "$UNIT_DIR/$1.service" <<EOF
[Unit]
Description=$2
Documentation=file://$STACK_DIR/docs/stack-watch.md

[Service]
Type=oneshot
ExecStart="$PYTHON" "$WATCH" $3
# The user manager's PATH is minimal; make sure docker is on it.
Environment=PATH=$(dirname "$DOCKER"):/usr/local/bin:/usr/bin:/bin
TimeoutStartSec=10min
EOF
}

write_service "$CHECK" "Media stack watch: containers, Seerr patch, Jellyfin, free space and host heartbeat" check
write_service "$STALLED" "Media stack watch: wanted titles that never download" stalled

cat > "$UNIT_DIR/$CHECK.timer" <<EOF
[Unit]
Description=Media stack watch every 15 minutes

[Timer]
# Must match CHECK_INTERVAL in stack_watch.py, which decides when two sightings are consecutive.
OnCalendar=*:0/15
AccuracySec=1min
# Run on wake or boot if a check was due while the machine was asleep or off.
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/$STALLED.timer" <<EOF
[Unit]
Description=Daily digest of wanted titles that never download

[Timer]
OnCalendar=*-*-* 10:00:00
RandomizedDelaySec=10min
# Run on wake or boot if the digest was due while the machine was asleep or off.
Persistent=true

[Install]
WantedBy=timers.target
EOF

was_enabled="$(systemctl --user is-enabled "$CHECK.timer" 2>/dev/null || true)"
systemctl --user daemon-reload

fail() {  # message
    echo "$1" >&2
    if [ "$was_enabled" = enabled ]; then
        echo "The timers from the previous install are still enabled, and now run this version." >&2
    else
        "$PYTHON" "$WATCH" disarm >/dev/null ||
            echo "Also run: python3 $WATCH disarm (a test check may have scheduled the real alert)" >&2
    fi
    exit 1
}

for unit in "$STALLED" "$CHECK"; do
    echo "Test run: $unit.service"
    if ! systemctl --user start "$unit.service"; then
        journalctl --user -u "$unit.service" --no-pager -n 30 >&2
        fail "$unit.service failed, so no timer was enabled. Fix the error above and re-run."
    fi
done

systemctl --user enable --now "$CHECK.timer" "$STALLED.timer" || fail "Could not enable the timers."

if [ "$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null)" != yes ]; then
    echo >&2
    echo "WARNING: linger is off, so these timers stop whenever you log out. To keep them running:" >&2
    echo "    sudo loginctl enable-linger $(id -un)" >&2
fi

echo
echo "Installed and enabled. Schedule:"
systemctl --user list-timers "$CHECK.timer" "$STALLED.timer" --no-pager
echo
echo "Logs:          journalctl --user -u 'media-stack-*'"
echo "Force alerts:  see docs/stack-watch.md, Testing"
