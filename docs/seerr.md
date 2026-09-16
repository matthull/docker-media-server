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
   ([jellyfin#16097](https://github.com/jellyfin/jellyfin/issues/16097), open).

The Sonarr Scan sidesteps both: it derives availability from Sonarr's own
`statistics.episodeFileCount` and never consults Jellyfin. It also completes the request itself — the
`request` row flips to Completed inside the same save, via a subscriber, not a later job. The
maintainers endorse this route ([#960](https://github.com/seerr-team/seerr/issues/960): *"You can
change the frequency of these jobs. Sonarr/radarr scan job will mark them available"*), though no
one has documented running it at a short interval, so treat the cadence as sanctioned but not
well-trodden.

**Order matters, and it is self-correcting.** Once a season row reaches Available, no later scan can
downgrade it — the scanner short-circuits on the stored status. A Jellyfin scan that runs *first* on
a new series can knock a season from Processing back to Unknown, but the next Sonarr Scan restores
it within 5 minutes.

**Known gap:** the Sonarr Scan does not set `jellyfinMediaId`, so a brand-new title reads Available
with no "Play on Jellyfin" link until a Jellyfin scan runs (nightly by default). Availability is
correct throughout; only the deep link is missing.

**Do not raise this past ~1,000 series without re-timing it.** Each run walks *every* series with no
change detection, paced at roughly `ceil(series / 50) × 4s` — about 4s at small libraries, but ~4m46s
measured at 2,352 series. There is no re-entrancy guard: a run that outlasts its interval is aborted
by the next one, which restarts from the beginning. Rows already written are kept, so nothing
corrupts, but the *tail of the list is then never reached* and those titles silently stop resolving.
Above ~2,000 series use 15 minutes. (The 1,000/2,000 figures are interpolated from two measurements,
not measured across the range.) TMDB is not a constraint — lookups are cached 6-12h, so cadence and
API volume are decoupled.

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
