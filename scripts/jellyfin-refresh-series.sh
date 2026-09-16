#!/usr/bin/env bash
# Heals a brand-new series that Jellyfin imported but stranded.
#
# THE BUG (jellyfin#16097). Jellyfin joins a series to its seasons and episodes on
# SeriesPresentationUniqueKey. That key is rewritten on the *series* once provider
# identification finishes, but children created before that moment keep the old value, so the
# join finds nothing. The episodes are in the library — a flat /Items query lists them with the
# right SeriesId — but GET /Shows/{id}/Seasons and /Shows/{id}/Episodes both return 0.
#
# WHY THAT IS USER-VISIBLE, not just bookkeeping. Those two endpoints are what every Jellyfin
# client renders a series page from (the web client's itemDetails calls getSeasons(item.Id)).
# A stranded series shows its poster and its overview and offers nothing to play. Seerr also
# counts episodes with them, so it reports the request unavailable while the files sit on disk.
#
# WHY IT DOES NOT ALWAYS SELF-HEAL. Each import fires the arr's Jellyfin connector, and each
# fire refreshes the series, rewriting the children's keys. Episodes that trickle in over
# several batches therefore heal themselves within 5-15 minutes. A season pack imports every
# episode in ONE batch, so there is no second fire and nothing heals it. Sonarr prefers season
# packs, which makes the unrecoverable case the common one. Left alone it takes ~36h.
#
# WHAT FIXES IT. A metadata FullRefresh on the series item. Measured here: a stranded series heals
# within ~5-20s of the POST, and `Default` instead of `FullRefresh` does not fix it. The mechanism
# is presumably that the refresh cascades from the series to its children and rewrites their keys,
# but that is inferred from the behaviour, not read out of Jellyfin's source.
#
# TWO TRAPS, both of which produce a call that returns 204 and does nothing:
#
#   1. There is NO `recursive` parameter on POST /Items/{id}/Refresh. Jellyfin 10.11's OpenAPI
#      spec accepts only metadataRefreshMode, imageRefreshMode, replaceAllMetadata,
#      replaceAllImages and regenerateTrickplay. Guides (and jellyfin#17293) tell you to send
#      Recursive=true; ASP.NET Core silently drops unknown query parameters, so that call is
#      really just a FullRefresh. Sending it does no harm but it explains nothing — do not
#      "restore" it believing it is what makes the fix work.
#   2. Refreshing too early re-reads the STALE key and achieves nothing. The refresh has to land
#      after provider identification, hence REFRESH_DELAY below.
#
# See docs/sonarr.md and the CLAUDE.md operational trap for the full history.
#
# Installed by scripts/install-jellyfin-refresh.sh, which also registers it with Sonarr. It is
# not meant to be run by hand, but doing so is harmless: with no sonarr_* variables set it
# reports usage and exits non-zero.

set -uo pipefail

VERSION=1

: "${JELLYFIN_URL:=http://host.docker.internal:8096}"
: "${JELLYFIN_API_KEY:=}"
# Seconds to wait for provider identification before looking. Too short and the refresh reads
# the stale key and silently does nothing, which is the failure this script exists to prevent.
: "${REFRESH_DELAY:=60}"
# How long to keep polling /Shows/{id}/Seasons after the refresh before declaring it failed.
: "${REFRESH_TIMEOUT:=180}"
# How long to keep looking for the series in Jellyfin before giving up. The connector's own
# debounce is 60s, so this has to comfortably exceed it.
: "${LOOKUP_TIMEOUT:=180}"
: "${STATE_DIR:=/config/jellyfin-refresh}"
# Set to 1 to detect and log without POSTing the refresh. Used to observe the stranded state.
: "${REFRESH_DRYRUN:=0}"
# Truncate the detached worker's log once it passes this, so it cannot grow without bound in
# the config volume. A few lines per import means this holds months of history.
: "${LOG_MAX_BYTES:=1048576}"

LOG_FILE="$STATE_DIR/jellyfin-refresh.log"

log() { printf '%s jellyfin-refresh[%s]: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$*"; }

# curl wrapper that separates body from status so a 401 can't be mistaken for a 0 count.
# Prints "<status>\n<body>". Never prints the API key.
jf_call() {  # method, path-with-query
    curl -sS -X "$1" \
        -H "X-Emby-Token: $JELLYFIN_API_KEY" \
        -H 'Accept: application/json' \
        --max-time 30 \
        -w '\n%{http_code}' \
        "$JELLYFIN_URL/$2" 2>/dev/null
}

# Echoes body, returns the status via $JF_STATUS. Note that $JF_STATUS only survives when jf is
# called directly; inside $( ) it is set in a subshell and lost, which is why season_count below
# reports its status through its stdout instead.
JF_STATUS=000
jf() {
    local out
    out="$(jf_call "$@")" || { JF_STATUS=000; printf ''; return 1; }
    JF_STATUS="${out##*$'\n'}"
    printf '%s' "${out%$'\n'*}"
    [ "$JF_STATUS" -ge 200 ] && [ "$JF_STATUS" -lt 300 ]
}

# Number of seasons Jellyfin can actually join to this series. This IS the defect: it is what
# clients render from, so it is what we measure, rather than inferring health from the import.
#
# Prints "<count> <http status>". A failed call reports -1 rather than 0, because a 401 or a 503
# parsed as "zero seasons" would look exactly like stranding and trigger an endless refresh.
season_count() {  # series item id
    local body count
    if ! body="$(jf GET "Shows/$1/Seasons")"; then
        printf -- '-1 %s' "$JF_STATUS"
        return 1
    fi
    count="$(printf '%s' "$body" | jq -r 'if has("TotalRecordCount") then .TotalRecordCount else -1 end' 2>/dev/null)"
    case "$count" in
        ''|*[!0-9-]*) count=-1 ;;
    esac
    printf '%s %s' "$count" "$JF_STATUS"
}

# Resolve the Sonarr series to a Jellyfin item id.
#
# Matching on TVDB id alone is not enough: before identification completes the item has no
# provider ids at all, and that window is exactly when this script runs. So fall back to the
# library folder name, which Sonarr and Jellyfin always agree on because Sonarr created it.
# Path comes back as a host path and sonarr_series_path is a container path, so compare only
# the final component.
find_series_id() {  # tvdb id, series folder basename
    local tvdb="$1" folder="$2" body
    body="$(jf GET 'Items?IncludeItemTypes=Series&Recursive=true&Fields=Path,ProviderIds&EnableTotalRecordCount=false')" || return 1
    printf '%s' "$body" | jq -r --arg tvdb "$tvdb" --arg folder "$folder" '
        [ .Items[]?
          | select(
              ($tvdb != "" and (.ProviderIds.Tvdb // "" | tostring) == $tvdb)
              or ($folder != "" and ((.Path // "") | sub("/+$";"") | split("/") | last) == $folder)
            )
          | .Id ] | first // empty
    ' 2>/dev/null
}

main() {
    local event="${sonarr_eventtype:-}"

    if [ -z "$event" ]; then
        echo "This is a Sonarr Custom Script connector, not a standalone command." >&2
        echo "Install it with scripts/install-jellyfin-refresh.sh; see docs/sonarr.md." >&2
        return 2
    fi

    # Sonarr calls with eventtype=Test when you save the connector. Prove the credentials and
    # the route work now, so a typo surfaces at save time instead of on the first real import.
    if [ "$event" = "Test" ]; then
        log "test: version $VERSION, JELLYFIN_URL=$JELLYFIN_URL, delay=${REFRESH_DELAY}s"
        if [ -z "$JELLYFIN_API_KEY" ]; then
            log "test FAILED: JELLYFIN_API_KEY is empty"
            return 1
        fi
        if jf GET 'System/Info' >/dev/null; then
            log "test OK: Jellyfin reachable and key accepted (HTTP $JF_STATUS)"
            return 0
        fi
        # A bare 200 with an HTML body means the key was ignored; jf already treats only 2xx as
        # success, and Jellyfin answers /System/Info with 401 for a bad key, so this is honest.
        log "test FAILED: Jellyfin returned HTTP $JF_STATUS for /System/Info"
        return 1
    fi

    # ImportComplete fires once per import batch; Download fires once per episode file. We
    # accept both so the connector works however it was registered, and the lock below collapses
    # the duplicates either way.
    case "$event" in
        Download|ImportComplete) ;;
        *) log "ignoring event $event"; return 0 ;;
    esac

    local series_id="${sonarr_series_id:-}"
    local tvdb="${sonarr_series_tvdbid:-}"
    local path="${sonarr_series_path:-}"
    local title="${sonarr_series_title:-unknown}"
    local folder="${path##*/}"

    if [ -z "$series_id" ] && [ -z "$tvdb" ] && [ -z "$folder" ]; then
        log "no series identity in environment for event $event; nothing to do"
        return 0
    fi

    if [ -z "$JELLYFIN_API_KEY" ]; then
        log "JELLYFIN_API_KEY is not set; cannot refresh '$title'"
        return 1
    fi

    mkdir -p "$STATE_DIR" 2>/dev/null

    # Sonarr runs a custom script synchronously and waits for it. This one deliberately sleeps
    # through provider identification and then polls, so in the foreground it would hold up the
    # import pipeline for minutes and risk being killed mid-refresh. Hand the work to a session
    # of its own -- setsid, not a bare &, so it survives Sonarr reaping the process group -- and
    # return to Sonarr immediately. The child re-enters here with REFRESH_CHILD set.
    if [ "${REFRESH_CHILD:-0}" != 1 ]; then
        if [ -s "$LOG_FILE" ] && [ "$(wc -c <"$LOG_FILE" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
            tail -c "$((LOG_MAX_BYTES / 2))" "$LOG_FILE" >"$LOG_FILE.tmp" 2>/dev/null &&
                mv "$LOG_FILE.tmp" "$LOG_FILE"
        fi
        REFRESH_CHILD=1 setsid "$0" >>"$LOG_FILE" 2>&1 &
        log "dispatched background worker for '$title'; follow it in $LOG_FILE"
        return 0
    fi

    # One worker per series. A season pack that imports 6 files fires this script 6 times within
    # a second or two; without the lock they would all sleep and then all refresh. -n means the
    # losers exit immediately rather than queueing up behind a 60s sleep.
    local lock="$STATE_DIR/${series_id:-$tvdb}.lock"
    exec 9>"$lock" || { log "cannot open lock $lock"; return 1; }
    if ! flock -n 9; then
        log "another worker already holds '$title'; skipping duplicate"
        return 0
    fi

    log "'$title' (sonarr id ${series_id:-?}, tvdb ${tvdb:-?}): waiting ${REFRESH_DELAY}s for provider identification"
    sleep "$REFRESH_DELAY"

    local item_id deadline
    deadline=$(( $(date +%s) + LOOKUP_TIMEOUT ))
    while :; do
        item_id="$(find_series_id "$tvdb" "$folder")"
        [ -n "$item_id" ] && break
        if [ "$(date +%s)" -ge "$deadline" ]; then
            log "'$title' not found in Jellyfin after ${LOOKUP_TIMEOUT}s (tvdb=${tvdb:-none} folder='$folder'); giving up"
            return 1
        fi
        sleep 10
    done

    local seasons status
    read -r seasons status <<<"$(season_count "$item_id")"
    if [ "$seasons" -gt 0 ]; then
        log "'$title' ($item_id) already has $seasons season(s); no refresh needed"
        return 0
    fi
    if [ "$seasons" -lt 0 ]; then
        log "'$title' ($item_id): could not read season count (HTTP $status); refreshing anyway"
    else
        log "'$title' ($item_id) is STRANDED: /Shows/$item_id/Seasons returns 0"
    fi

    # Built once so the dry run cannot describe a different call from the one that really goes
    # out. No `recursive` parameter exists (see header); FullRefresh on the series is what
    # cascades to the children, and that cascade is the entire fix.
    local refresh="Items/$item_id/Refresh?metadataRefreshMode=FullRefresh&imageRefreshMode=FullRefresh&replaceAllMetadata=false&replaceAllImages=false"

    if [ "$REFRESH_DRYRUN" = 1 ]; then
        log "dry run: would POST /$refresh"
        return 0
    fi

    if jf POST "$refresh" >/dev/null; then
        log "'$title' ($item_id): refresh accepted (HTTP $JF_STATUS)"
    else
        log "'$title' ($item_id): refresh REJECTED (HTTP $JF_STATUS)"
        return 1
    fi

    # 204 only means Jellyfin queued the work. Confirm the thing we actually care about.
    deadline=$(( $(date +%s) + REFRESH_TIMEOUT ))
    while :; do
        sleep 5
        read -r seasons status <<<"$(season_count "$item_id")"
        if [ "$seasons" -gt 0 ]; then
            log "'$title' ($item_id): HEALED, $seasons season(s) now visible"
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            log "'$title' ($item_id): STILL STRANDED ${REFRESH_TIMEOUT}s after a refresh that returned 204." \
                "Jellyfin accepted the refresh but the season join is still empty - this is not the" \
                "known stale-key case and needs a look. Repair by hand with a full library scan."
            return 1
        fi
    done
}

main "$@"
