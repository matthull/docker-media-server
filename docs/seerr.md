# Seerr

Request portal. People browse and request here; Seerr routes to Radarr/Sonarr.

**Web UI:** `http://<host>:5055`

## First-Time Setup

1. **Connect Jellyfin** — server URL and an API key from Jellyfin > Dashboard > API Keys. This is how
   Seerr knows what you already have.
2. **Connect Radarr** — host `radarr`, port `7878`, API key. Pick a default quality profile and root
   folder.
3. **Connect Sonarr** — host `sonarr`, port `8989`, same idea.
4. **User Management** — Settings > Users, for auto-approval and per-user limits.

Use container names, never `localhost`.

## TV requests: the Sonarr scan resolves them, the Jellyfin scan cannot

**A TV request will never be marked available by Seerr's Jellyfin scanning.** Set the **Sonarr Scan**
job to run every 5 minutes (Settings > Jobs & Cache, or
`POST /api/v1/settings/jobs/sonarr-scan/schedule {"schedule":"0 */5 * * * *"}`). Without it a show is
watchable in Jellyfin within a minute while the requester still reads "Processing" for up to a day.

Two independent upstream bugs cause this, and neither has a fix to wait for:

1. **The 5-minute "Jellyfin Recently Added Scan" is a permanent no-op for TV.** It calls
   `GET /Items/Latest`, which for a `tvshows` library returns `Type=Episode` — Jellyfin hardcodes
   `BaseItemKind.Episode` there, and surfaces the parent `Series` only when 2+ episodes of it land in
   the same 12-item window. Seerr's `processItem()` dispatches on `Movie` or `Series` only and drops
   everything else with no log line. Movies are unaffected, which is why they flip in 60s and TV
   doesn't. Unchanged in Jellyfin 10.8 through 12.x, so no version helps, and no query parameter
   fixes it (`groupItems=false` is worse; `includeItemTypes=Series` queries the show's own
   `DateCreated` and so misses existing shows gaining episodes). Still present in Seerr `develop`;
   the only tracked issue ([#177](https://github.com/seerr-team/seerr/issues/177)) was closed twice
   without addressing it.
2. **A brand-new series can have a broken hierarchy in Jellyfin** — `GET /Shows/{id}/Seasons` and
   `/Shows/{id}/Episodes`, the two endpoints Seerr counts episodes with, return `0` while the flat
   `/Items` query lists the episodes with the correct `SeriesId`. The join is on
   `SeriesPresentationUniqueKey`, which is rewritten on the series after provider identification but
   left stale on children created before it
   ([jellyfin#16097](https://github.com/jellyfin/jellyfin/issues/16097), open). **This is
   user-visible in Jellyfin itself, not just Seerr** — the web client's `itemDetails` page renders
   seasons from `/Shows/{id}/Seasons`, so a stranded series shows poster art but no seasons or
   episodes to play (verified against Jellyfin 10.11.11 web client source). **Season packs are the
   common trigger:** all episodes import at once in a single connector fire, so there is no subsequent
   fire to heal the stale keys. Individual episode imports self-heal in 5–15 min because later
   connector fires trigger series refreshes that update the children's keys. Sonarr prefers season
   packs when available, so the stranding case is the common one — worst-case resolution ≈36 h
   (Jellyfin's 12 h scan interleaved with Seerr's daily 03:00 cron). Cheapest known repair:
   `POST /Items/{seriesId}/Refresh?Recursive=true&MetadataRefreshMode=FullRefresh`
   ([jellyfin#17293](https://github.com/jellyfin/jellyfin/issues/17293)).

The Sonarr Scan sidesteps both: it derives availability from Sonarr's own
`statistics.episodeFileCount` and never consults Jellyfin. It also completes the request itself — the
`request` row flips to Completed inside the same save, via a subscriber, not a later job. The
maintainers endorse this route ([#960](https://github.com/seerr-team/seerr/issues/960): *"You can
change the frequency of these jobs. Sonarr/radarr scan job will mark them available"*), though no
one has documented running it at a short interval, so treat the cadence as sanctioned but not
well-trodden.

**Order matters, and it should be self-correcting.** Reading the scanner source, an Available season
row cannot be downgraded by a later scan — the status expression short-circuits on the stored value,
so every downgrade arm is unreachable once a season reaches Available. A Jellyfin scan that runs
*first* on a new series can knock a season from Processing back to Unknown, but the next Sonarr Scan
should restore it within 5 minutes. **This is a source read, not an observation** — no run here has
yet put a Jellyfin scan against a series whose hierarchy was still broken at the moment it ran, so
the guard has not been watched doing its job. Treat it as well-founded but unproven.

**Known gap:** the Sonarr Scan does not set `jellyfinMediaId`, so a brand-new title reads Available
with no "Play on Jellyfin" link until a Jellyfin scan runs (nightly by default). Availability is
correct throughout; only the deep link is missing.

**This is a Seerr setting, not Compose config — it does not travel with this repo.** It lives in
Seerr's own config volume, so moving the stack to another machine, or restoring from a backup that
skips that volume, silently reverts TV to "Processing" while every container still reports healthy.
Re-apply it after any migration and check `GET /api/v1/settings/jobs`.

**Do not raise this past ~1,000 series without re-timing it.** Each run walks *every* series with no
change detection. Its pacing *floor* is `ceil(series / 50) × 4s` — one fixed 4s sleep per bundle of
50 — but real runs land well above the floor, because per-series work adds to it. The two figures
this rests on: 4.07s measured here at 1 series (which is just the single 4s sleep, so it says nothing
about the slope), and a report of 2,352 series whose floor is 192s but which took **4m46s actual**,
roughly 50% over floor. Above ~2,000 series use 15 minutes.

**Both the ~1,000/~2,000 ceiling and the 50%-over-floor factor are interpolated from those two
points. Nobody has measured anything in between.** The 2,352-series figure is a user report on the
Seerr tracker ([#3307](https://github.com/seerr-team/seerr/issues/3307) discussion), not a
measurement taken on this stack — if the ceiling ever matters to you, re-time it rather than trusting
this line.

There is no re-entrancy guard: a run that outlasts its interval is aborted by the next one, which
restarts from the beginning. Rows already written are kept, so nothing corrupts, but the *tail of the
list is then never reached* and those titles silently stop resolving — a failure that looks like
success, because the front of the library keeps working. TMDB is not a constraint: lookups are cached
6-12h, so cadence and API volume are decoupled.

## Things to Know

- **Behind a reverse proxy or tunnel, set two things** — Settings > General > *Enable Proxy Support*
  (otherwise every request appears to come from the Docker bridge, making rate limiting and login
  logs useless), and *Application URL*, so notification links aren't LAN addresses. Restart after.
- **The config directory is `config/jellyseerr`**, a leftover of the rename. Any `overseerr/` or
  `jellyseerr.bak/` siblings are stale.
- **IPv6** — if hostnames won't resolve, force IPv4 under Settings > Networking > Advanced.

## Links

- [Seerr Docs](https://docs.seerr.dev/) · [Troubleshooting](https://docs.seerr.dev/troubleshooting)
- [GitHub](https://github.com/seerr-team/seerr)
