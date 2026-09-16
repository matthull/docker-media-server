# Seerr

Request portal. People browse and request here; Seerr routes to Radarr/Sonarr.

**Web UI:** `http://<host>:5055`

## First-Time Setup

1. **Connect Jellyfin** — server URL and an API key from Jellyfin > Dashboard > API Keys. This is how
   Seerr knows what you already have. For a Jellyfin on the same Linux host the address is
   `host.docker.internal`, port `8096`; otherwise see
   [Reaching a Jellyfin that is not in this stack](jellyfin-library-updates.md#reaching-a-jellyfin-that-is-not-in-this-stack).
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
   (Jellyfin's 12 h scan interleaved with Seerr's daily 03:00 cron). Repair:
   `POST /Items/{seriesId}/Refresh?metadataRefreshMode=FullRefresh`. **Now automatic** — see
   [docs/sonarr.md](./sonarr.md) and `scripts/install-jellyfin-refresh.sh`. Note there is no
   `recursive` parameter on that endpoint despite what
   [jellyfin#17293](https://github.com/jellyfin/jellyfin/issues/17293) says; it is silently dropped,
   and the `FullRefresh` cascade from the series is what actually repairs the children.

The Sonarr Scan sidesteps both: it derives availability from Sonarr's own
`statistics.episodeFileCount` and never consults Jellyfin. It also completes the request itself — the
`request` row flips to Completed inside the same save, via a subscriber, not a later job. The
maintainers endorse this route ([#960](https://github.com/seerr-team/seerr/issues/960): *"You can
change the frequency of these jobs. Sonarr/radarr scan job will mark them available"*), though no
one has documented running it at a short interval, so treat the cadence as sanctioned but not
well-trodden.

**Order matters, and it is self-correcting — now observed, not just read from source.** An Available
season row cannot be downgraded by a later scan — the status expression short-circuits on the stored
value, so every downgrade arm is unreachable once a season reaches Available. Verified live: a
season-pack import (all episodes land in one connector-triggered batch, so the series/episode
hierarchy is still broken — see Defect 2 below) was run through **three separate `jellyfin-full-scan`
triggers while genuinely still broken**, and the season stayed at Available across all three. A
Jellyfin scan that runs *first* on a brand-new series can knock a season from Processing back to
Unknown, but the next Sonarr Scan restores it within 5 minutes.

**Known gap, corrected:** the Sonarr Scan does not set `jellyfinMediaId`, so a brand-new title reads
Available with no "Play on Jellyfin" link. This does **not** reliably close itself on the nightly
`jellyfin-full-scan` for season-pack arrivals — in the same live test, **two consecutive full scans
left the hierarchy broken and `jellyfinMediaId` unset.** Only the targeted per-item refresh,
`POST /Items/{seriesId}/Refresh?metadataRefreshMode=FullRefresh`,
actually resolved it (confirmed within ~20s). Since Sonarr prefers season packs when available, this
is the common case, not the rare one — the deep-link gap can persist indefinitely, not just "until
nightly." Availability itself is unaffected; only the Play-on-Jellyfin deep link is at risk. That
refresh is now fired automatically on import by the Sonarr Custom Script connector installed with
`scripts/install-jellyfin-refresh.sh` — see [docs/sonarr.md](./sonarr.md).

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

## "Unable to get queue from Radarr/Sonarr server" — raise `apiRequestTimeout` to 45000

The Download Tracker logs `Unable to get queue from Radarr server: Radarr` (and the Sonarr twin) on
its one-minute cycle.

**Correction to what the requester sees.** This section used to say they see no progress at all. That
is wrong: the shared `catch` only logs, `this.radarrServers[server.id]` is assigned solely on success
and never cleared on failure, and `resetDownloadTracker()` is a separate job on `0 0 1 * * *`. So a
failed cycle **freezes** the card on the previous snapshot — a stale percentage and a stale ETA —
until a cycle succeeds. It shows nothing at all only for a download whose first tracker cycles all
failed, which is the "Processing until the title appears" case. A confidently wrong ETA is arguably
worse than a blank one.

Set **Settings > Networking > API request timeout to 45000** (`network.apiRequestTimeout`
in `config/jellyseerr/settings.json`) and restart the container.

**Why it happens.** The tracker calls `refreshMonitoredDownloads()` — `POST /api/v3/command`, a
database **write** — before `getQueue()` on every cycle. When an arr's own work (imports, searches,
RSS sync) holds the SQLite write lock, that POST blocks until the lock frees. Both arrs are already
`journal_mode=wal`, so this is not a mode you can tune away; reads are unaffected, and a `GET /queue`
measured **3-5 ms while a write lock was held**. Only the write blocks. Measured by holding the lock
with `BEGIN IMMEDIATE` and timing the POST:

| Write lock held | Sonarr `POST /command` | Radarr `POST /command` |
| --------------- | ---------------------- | ---------------------- |
| 6 s             | 5.71 s → 201           | 5.79 s → 201           |
| 20 s            | 19.74 s → 201          | 19.75 s → 201          |
| 35 s            | **30.08 s → 500**      | 34.74 s → 201          |
| 45 s            | **30.07 s → 500**      | 44.76 s → 201          |
| 60 s            | **30.10 s → 500**      | 59.73 s → 201          |

Response time tracks the lock hold almost exactly — so a bigger timeout really does convert these
into successes, up to a point.

**Use 45000, not 30000.** Under that synthetic single-holder conflict Sonarr (4.0.19) gives up and
returns **HTTP 500 at 30.07-30.10 s**, reproducible across three trials, so a 30000 client deadline
races it by ~75 ms — Seerr aborts first and logs a *timeout*, hiding a real HTTP 500 behind what
looks like a network fault. It also throws away the 30-45 s band on Radarr, which has no such ceiling.
45000 keeps every success 30000 would get and adds Radarr's band.

**Do not read that 30 s figure as a law of Sonarr.** It is the behaviour under one externally-held
lock. Under real contention Sonarr has been seen returning **201 at 31.36 s**, past the "ceiling" —
the deadline is evidently per-attempt, not per-request, and real multi-writer retry dynamics differ.
Conversely an arr can exhaust its retries and 500 far *sooner* (one observed at 11 s after cycle
start), and no timeout helps against a 500.

**45000 is not sufficient under real download load, and this is the important measurement.** Ten
tracker cycles sampled while a season pack was actively downloading (6 items in Sonarr's queue, RSS
sync running):

| | `POST /command` max | over 10 s | over 30 s | `GET /queue` max |
| --- | --- | --- | --- | --- |
| Radarr | **66.35 s** | 4/10 | 2/10 | **0.009 s** |
| Sonarr | **31.36 s** | 3/10 | 1/10 | **0.008 s** |

Three consecutive live cycles failed at exactly 45.01 s — Seerr's new timeout — during that download.
So raising the timeout converts the common 10-30 s stalls into successes and still loses the tail,
and it loses it *precisely when a requester is most likely to be watching*, because the contention is
caused by the download they are waiting on.

**The real conclusion: `GET /queue` never blocks.** Across every measurement above — synthetic lock
held, real season pack downloading, both arrs — the read never exceeded **9 ms**. Only the write
Seerr does not need is ever slow. No timeout value is the right answer to this; not issuing the write
is. Treat 45000 as a palliative that covers the common case, not as a fix.

The upstream fix — stop issuing the write, poll for command completion with a deadline, and share one
in-flight promise across concurrent syncs — is [PR #3473](https://github.com/seerr-team/seerr/pull/3473),
closed as a duplicate of [PR #1055](https://github.com/seerr-team/seerr/pull/1055), which is a
one-line `pageSize` change that does not touch this code path. **Nothing is landing upstream.**

### The fix: Seerr is built here, not pulled

`images/seerr/Dockerfile` builds Seerr from the pinned upstream digest and deletes the two
`await <arr>.refreshMonitoredDownloads();` lines from `dist/lib/downloadtracker.js`. Nothing else
changes; the read is untouched.

The obvious objection to patching inside an image is that a Renovate bump reverts it silently, with
every container still reporting healthy. Three things make that impossible here, and
[`images/seerr/README.md`](../images/seerr/README.md) is the full account:

- the patch is in git, so it travels with the repo;
- the patch script asserts the call sites before and after editing and **fails the build** if it
  cannot patch, so no unpatched image is ever produced — the last good one keeps serving **if Seerr
  is already running**. On a cold start (after `docker compose down`, on a fresh clone or a new host)
  the failed build aborts the whole `up -d` and **no service in the stack starts**. The
  `Build seerr image` workflow builds the image on every pull request and push to `main` that touches
  it, so that failure shows up as a red check instead; a red run on `main` means do not `down` or
  migrate the stack until it is fixed. `images/seerr/README.md` has the recovery commands;
- `pull_policy: build` makes a plain `docker compose up -d` rebuild from the pinned base, so a bump
  actually reaches the container instead of sitting unused. **Not absolute:** `up -d --pull always`
  and `up -d --no-build` both skip the build silently and keep the old image running (verified, exit
  0, no warning). That yields a stale base, never an unpatched Seerr — but update with a plain
  `docker compose up -d`, because nothing detects a stale base.

An unpatched Seerr, reached some other way (an `image:` key put back, an override pointing at
upstream), *is* detected: [stack watch](stack-watch.md) reads the tracker out of the running container
every 15 minutes and alerts if the call is back.

Dropping the write is safe because **both arrs already run `RefreshMonitoredDownloads` themselves
every 1 minute** (`GET /api/v3/system/task`), the same cadence as Seerr's own Download Sync job.

Measured before and after on the same host, minutes apart, with
`local/seerr-timeout/cycletest.py 50` holding both arr write locks across a Download Sync boundary:

| | contended cycles clean |
| --- | --- |
| v3.4.1 as shipped, `apiRequestTimeout` 45000 | **0 / 2** (Sonarr 30.1 s → 500, Radarr 45.02 s → timeout) |
| built from `images/seerr/` | **3 / 3** |

Confirmed at the protocol level too: the arrs' command history shows Seerr's `trigger=manual`
`RefreshMonitoredDownloads` stopping at the exact cycle the patched container started, with only
`trigger=scheduled` after it.

A real movie request landed while this was being verified, and the patched tracker read the
**non-empty** queue (`Found 1 item(s) in progress on Radarr server`), with `GET /api/v1/request`
returning the populated `media.downloadStatus` the request card renders from — release name,
`status=downloading`, size, `timeLeft`, ETA.

**Still unobserved:** nobody has yet watched a progress bar in a *browser*, and that sample was
caught at ~100%, so a changing percentage was never watched either. The data path is now observed;
the rendering is still inferred.

**Keep `apiRequestTimeout` at 45000 anyway.** It no longer protects the download tracker, but it
covers every *other* Seerr→arr call that takes the write lock — notably adding a newly approved
request to Sonarr or Radarr, which contends with exactly the same imports.

**That setting is Seerr config, not Compose config — it does not travel with this repo**, exactly like
the Sonarr Scan schedule above. It is the *second* setting living only in the jellyseerr config
volume; check both after any migration or restore. The download-tracker patch itself is no longer in
that category — it is in git now.

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
