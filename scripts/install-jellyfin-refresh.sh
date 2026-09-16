#!/bin/bash
# Installs jellyfin-refresh-series.sh into Sonarr and registers the Connect notification that
# fires it. See docs/sonarr.md, "Brand-new series arrive with no episodes".
#
#     ./scripts/install-jellyfin-refresh.sh               install, or update in place
#     ./scripts/install-jellyfin-refresh.sh --check       report drift, change nothing
#     ./scripts/install-jellyfin-refresh.sh --uninstall   remove script and notification
#
# The script is copied into Sonarr's config volume rather than bind-mounted, because
# docker-compose.override.yml.example replaces sonarr's whole `volumes:` list, so a mount added
# in docker-compose.yml would silently vanish on any host using the override -- which is every
# host that wants hardlinks. /config is the one path both spellings always share.
#
# That copy is the one piece of this fix that lives outside git, so --check exists to tell you
# when the deployed copy has drifted from the repo. Re-run the installer after a git pull.
#
# JELLYFIN_URL and JELLYFIN_API_KEY reach the script through the sonarr container's environment
# (docker-compose.yml passes them from .env), so `docker compose up -d sonarr` must have been run
# since those were added. This script checks and tells you if not.

set -euo pipefail

STACK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$STACK_DIR/scripts/jellyfin-refresh-series.sh"
NAME="Jellyfin refresh on new series"
DEST_IN_CONTAINER=/config/jellyfin-refresh-series.sh
CONTAINER=sonarr
MODE="${1:-install}"

case "$MODE" in install|--check|--uninstall) ;; *) echo "Usage: $0 [--check|--uninstall]" >&2; exit 2 ;; esac

die() { echo "$*" >&2; exit 1; }

[ -f "$SRC" ] || die "Not found: $SRC"
command -v docker >/dev/null || die "docker is not on PATH."
command -v jq >/dev/null || die "jq is not installed."
docker inspect "$CONTAINER" >/dev/null 2>&1 || die "No '$CONTAINER' container. Start the stack first."

# Sonarr's own key, read from the config it actually uses rather than from a settings file that
# may have drifted.
CONFIG_ROOT="$(grep -E '^CONFIG_ROOT=' "$STACK_DIR/.env" | cut -d= -f2-)"
[ -n "$CONFIG_ROOT" ] || die "CONFIG_ROOT is not set in $STACK_DIR/.env"
SONARR_XML="$CONFIG_ROOT/config/sonarr/config.xml"
[ -f "$SONARR_XML" ] || die "Not found: $SONARR_XML"
API_KEY="$(grep -oP '(?<=<ApiKey>)[^<]*' "$SONARR_XML")"
URL_BASE="$(grep -oP '(?<=<UrlBase>)[^<]*' "$SONARR_XML" || true)"
PORT="$(grep -oP '(?<=<Port>)[^<]*' "$SONARR_XML")"
API="http://localhost:${PORT}${URL_BASE}/api/v3"

sonarr_api() {  # method, path, [body]
    local method="$1" path="$2"
    if [ $# -ge 3 ]; then
        curl -sS -X "$method" "$API/$path" -H "X-Api-Key: $API_KEY" \
            -H 'Content-Type: application/json' -d "$3"
    else
        curl -sS -X "$method" "$API/$path" -H "X-Api-Key: $API_KEY"
    fi
}

sonarr_api GET system/status | jq -e '.version' >/dev/null 2>&1 ||
    die "Sonarr API did not answer at $API. Check UrlBase (see CLAUDE.md, trap 1)."

existing_id="$(sonarr_api GET notification |
    jq -r --arg n "$NAME" '.[] | select(.name == $n) | .id' | head -1)"

if [ "$MODE" = --uninstall ]; then
    [ -n "$existing_id" ] && sonarr_api DELETE "notification/$existing_id" >/dev/null &&
        echo "Removed the '$NAME' notification."
    docker exec "$CONTAINER" rm -f "$DEST_IN_CONTAINER" 2>/dev/null &&
        echo "Removed $DEST_IN_CONTAINER."
    echo "Left the log and lock files in /config/jellyfin-refresh/ for reference."
    exit 0
fi

# --- has the container been recreated since the env vars were added? ---------------
env_json="$(docker inspect "$CONTAINER" | jq -r '.[0].Config.Env')"
has_key="$(printf '%s' "$env_json" | jq -r 'map(select(startswith("JELLYFIN_API_KEY="))) | length')"
key_empty="$(printf '%s' "$env_json" | jq -r 'map(select(. == "JELLYFIN_API_KEY=")) | length')"

if [ "$MODE" = --check ]; then
    rc=0
    if docker exec "$CONTAINER" test -f "$DEST_IN_CONTAINER" 2>/dev/null; then
        if docker exec "$CONTAINER" cat "$DEST_IN_CONTAINER" | diff -q - "$SRC" >/dev/null; then
            echo "script:       up to date"
        else
            echo "script:       DRIFTED from $SRC -- re-run this installer"; rc=1
        fi
    else
        echo "script:       NOT INSTALLED"; rc=1
    fi
    [ "$has_key" != 0 ] && [ "$key_empty" = 0 ] &&
        echo "environment:  JELLYFIN_API_KEY present in $CONTAINER" ||
        { echo "environment:  JELLYFIN_API_KEY MISSING or empty in $CONTAINER -- set JELLYFIN_ARR_API_KEY in .env, then: docker compose up -d $CONTAINER"; rc=1; }
    if [ -n "$existing_id" ]; then
        sonarr_api GET "notification/$existing_id" |
            jq -r '"notification:  id \(.id), onImportComplete=\(.onImportComplete), onDownload=\(.onDownload), path=\(.fields[] | select(.name=="path") | .value)"'
    else
        echo "notification: NOT REGISTERED"; rc=1
    fi
    exit $rc
fi

# --- install ----------------------------------------------------------------------
[ "$has_key" != 0 ] && [ "$key_empty" = 0 ] || cat >&2 <<EOF
WARNING: $CONTAINER has no JELLYFIN_API_KEY in its environment, so the script will not be able
         to authenticate. Set JELLYFIN_ARR_API_KEY in $STACK_DIR/.env and then run:
             docker compose up -d $CONTAINER
         (a plain 'restart' is not enough -- the container must be recreated to pick up .env)
EOF

docker cp "$SRC" "$CONTAINER:$DEST_IN_CONTAINER"
docker exec -u 0 "$CONTAINER" chmod 0755 "$DEST_IN_CONTAINER"
echo "Installed $DEST_IN_CONTAINER"

# onImportComplete fires once per import batch; onDownload fires once per episode file. We
# register onImportComplete only -- the script's own per-series lock would collapse the
# duplicates anyway, but not generating six workers per season pack is cheaper and quieter.
payload="$(jq -n --arg name "$NAME" --arg path "$DEST_IN_CONTAINER" '{
    name: $name,
    implementation: "CustomScript",
    implementationName: "Custom Script",
    configContract: "CustomScriptSettings",
    onGrab: false, onDownload: false, onUpgrade: false, onImportComplete: true,
    onRename: false, onSeriesAdd: false, onSeriesDelete: false,
    onEpisodeFileDelete: false, onEpisodeFileDeleteForUpgrade: false,
    onHealthIssue: false, onHealthRestored: false, onApplicationUpdate: false,
    onManualInteractionRequired: false,
    includeHealthWarnings: false,
    fields: [ { name: "path", value: $path }, { name: "arguments", value: "" } ]
}')"

if [ -n "$existing_id" ]; then
    payload="$(printf '%s' "$payload" | jq --argjson id "$existing_id" '. + {id: $id}')"
    sonarr_api PUT "notification/$existing_id" "$payload" | jq -r '"Updated notification id \(.id)"'
else
    sonarr_api POST notification "$payload" | jq -r '"Created notification id \(.id)"'
fi

echo
# Two checks, because neither alone is worth much. Sonarr's test action proves Sonarr can find
# and execute the path it just stored. Running the script ourselves proves it can actually
# authenticate to Jellyfin -- and prints the reason when it can't, which Sonarr does not: it
# logs custom-script output only at debug level, so at the default level a broken key is silent.
echo "Checking Sonarr can execute it..."
sonarr_api POST "notification/test" "$payload" >/dev/null ||
    { echo "Sonarr could not run $DEST_IN_CONTAINER. Check: docker logs $CONTAINER" >&2; exit 1; }

echo "Checking it can reach Jellyfin..."
docker exec -e sonarr_eventtype=Test "$CONTAINER" "$DEST_IN_CONTAINER" ||
    { echo "The script could not reach Jellyfin -- see the reason above." >&2; exit 1; }

echo
echo "Done. Worker log: docker exec $CONTAINER cat /config/jellyfin-refresh/jellyfin-refresh.log"
echo "Drift check:     $0 --check"
