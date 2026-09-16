# Development Instructions

See [README](./README.md) for the project overview and setup, and [docs/](./docs/) for per-service
guides.

This repo is **public**. Anything describing one particular host — LAN IPs, drive letters, hardware,
library contents, provider accounts — goes in the gitignored files under
[Local, untracked files](#local-untracked-files), never in a tracked one.

## The one design goal

**Everything in this stack exists so a low-powered TV client direct-plays via Jellyfin**, and the
Jellyfin host never transcodes. Target library shape: 100% h264/MKV/SDR/1080p, EAC3/AC3/AAC audio,
text subtitles.

The reference client is a Fire TV Stick, standing in for cheap streaming hardware generally —
Chromecast/Google TV, Roku, budget Android TV boxes, built-in Samsung (no DTS since 2018) and LG (no
DTS 2020-2022) apps. An Nvidia Shield bitstreams DTS-HD/TrueHD and is the exception, but a shared
library must satisfy the *weakest* client on the account, so it loosens nothing.

Four things force a transcode, most expensive first. Judge every change against them:

1. **Image subtitles (PGS/VOBSUB)** → Jellyfin burns them in → *full video re-encode*. Decided
   **server-side, so it hits nearly every client** — not a Fire Stick limitation. Avoided
   structurally: the quality profiles allow WEB-DL/WEBRip only, and disc sources are where PGS lives.
2. **AV1** → the only genuinely per-device trigger, and it bites twice: the client has no AV1 decoder
   so the server must transcode, and if the server GPU also lacks one (e.g. VCN 2.0) that falls back
   to CPU. The Fire TV Stick **4K Max** decodes AV1; the plain 4K, 3rd-gen, Lite and 2nd-gen Cube do
   not, and it cannot be added in software. Record which stick is in use in `CLAUDE.local.md`.
3. **DTS / DTS-HD MA / DTS-X / DTS-ES / DTS-HD HRA** → audio transcode. On current Fire TV hardware
   only Kodi passes DTS-HD through; Jellyfin gets DTS core at best.
4. **TrueHD / TrueHD Atmos / FLAC / PCM** → audio transcode. Narrower support still than DTS.

`recyclarr.yml` scores all of these `-10000`, **deliberately overriding TRaSH's own rewards**
(DTS-HD MA +2500, TrueHD ATMOS +5000, FLAC/PCM +2250). Do not "restore guide defaults" on the audio
custom formats — that is the whole direct-play guarantee.

## Repository Structure

```
docker-compose.yml                  # Core: Sonarr, Radarr, Seerr, SABnzbd, Bazarr, Prowlarr, Recyclarr, Uptime Kuma, Tailscale, cloudflared
docker-compose.override.yml.example # Copy to docker-compose.override.yml — the /data mount, see below
backup/
  backup-config-local.sh            # Default: tarball to another local disk, no credentials
  install-local-backup-timer.sh     # Weekly systemd timer for the above
  backup-config.sh                  # Opt-in off-site: restic → S3-compatible, snapshots SQLite DBs
  install-restic-timer.sh           # Daily systemd timer for the above
  .env.example                      # Restic credentials (off-site only)
monitoring/
  stack_watch.py                    # ntfy alerts for dead containers, host offline, stalled requests
  test_stack_watch.py               # python3 -m unittest discover -s monitoring
  install-stack-watch.sh            # systemd --user timers for the above; see docs/stack-watch.md
images/
  seerr/Dockerfile                  # The one built-not-pulled service; see its README.md
  seerr/patch-downloadtracker.sh    # Drops the arr write that blocks Seerr's queue read
scripts/
  jellyfin-refresh-series.sh        # Sonarr Custom Script: heals a stranded brand-new series
  install-jellyfin-refresh.sh       # Installs the above into sonarr + registers the connector
  test_jellyfin_refresh.py          # python3 -m unittest discover -s scripts
  mutate_jellyfin_refresh.py        # Mutation check for the above; every mutant must be killed
docs/                               # Per-service setup guides
.env.example                        # TZ, PUID/PGID, MEDIA_ROOT, CONFIG_ROOT, SABNZBD_TEMP, TS_AUTHKEY, stack watch
```

There is no `extras/`. Every service in the repo is one this stack actually runs, so anything
documented here is verified against a live install rather than shipped on spec. Optional containers
hang off Compose profiles in the core file (`cloudflared` is the pattern), not a second stack.

### Local, untracked files

| Path                          | What it holds                                            |
| ----------------------------- | -------------------------------------------------------- |
| `local/`                      | Scratch space: audits, API dumps, host-specific notes    |
| `CLAUDE.local.md`             | Agent instructions for one host; loaded with `CLAUDE.md` |
| `docker-compose.override.yml` | Host-specific Compose tweaks                             |
| `.env`, `backup/.env`         | Secrets and paths                                        |
| `.claude/settings.json`       | Service API keys                                         |

## Networking, Volumes, Ports

All services share the `sofa-squad` bridge network (`172.18.0.0/16` by default), created by the core
stack. Address services by container name: `http://sonarr:8989`.

```
${CONFIG_ROOT}/config/<service>/    # Per-service config
${MEDIA_ROOT}/downloads/            # SABnzbd staging
${MEDIA_ROOT}/complete/tv/          # Sonarr root folder
${MEDIA_ROOT}/complete/movies/      # Radarr root folder
```

Those are host paths. `docker-compose.override.yml.example` mounts `${MEDIA_ROOT}` **once** as
`/data` inside sonarr/radarr/bazarr/sabnzbd (`/data/complete/tv`, `/data/downloads/completed`) rather
than separate `/tv`, `/movies` and `/downloads` mounts — separate mounts look like separate
filesystems to the container and break hardlinks. Rationale is in its header.

| Service  | Port | API Docs                                                               |
| -------- | ---- | ---------------------------------------------------------------------- |
| Seerr    | 5055 | [docs.seerr.dev](https://docs.seerr.dev/)                              |
| Sonarr   | 8989 | [wiki.servarr.com/sonarr/api](https://wiki.servarr.com/sonarr/api)     |
| Radarr   | 7878 | [wiki.servarr.com/radarr/api](https://wiki.servarr.com/radarr/api)     |
| SABnzbd  | 8080 | [sabnzbd.org/wiki/advanced/api](https://sabnzbd.org/wiki/advanced/api) |
| Bazarr   | 6767 | [wiki.bazarr.media](https://wiki.bazarr.media/)                        |
| Prowlarr | 9696 | [wiki.servarr.com/prowlarr/api](https://wiki.servarr.com/prowlarr/api) |
| Uptime Kuma | 3001 | [github.com/louislam/uptime-kuma](https://github.com/louislam/uptime-kuma/wiki) |

Env vars are documented in `.env.example`. `COMPOSE_PROFILES=cloudflare` plus `CF_TUNNEL_TOKEN`
enables the tunnel; either alone does nothing.

**Jellyfin is deliberately not in this stack** — it runs on the media host itself so it can use the
GPU directly. From a container, reach it at its host's LAN address; see
[docs/cloudflared.md](./docs/cloudflared.md) for why the obvious alternatives are wrong under WSL.
Record the actual address in `CLAUDE.local.md`.

## Agent API Access

API keys live in `.claude/settings.json` (gitignored); copy `.claude/settings.example.json`. Each
entry carries `url`, `urlBase`, and `apiVersion` where it varies, so paths can be built without
rediscovering them.

| Service  | Request                                                          | Auth                               |
| -------- | ---------------------------------------------------------------- | ---------------------------------- |
| Sonarr   | `GET {url}/sonarr/api/v3/{endpoint}`                             | `X-Api-Key` header                 |
| Radarr   | `GET {url}/radarr/api/v3/{endpoint}`                             | `X-Api-Key` header                 |
| Prowlarr | `GET {url}/api/v1/{endpoint}`                                    | `X-Api-Key` header                 |
| Bazarr   | `GET {url}/api/{endpoint}`                                       | `X-API-KEY` header (note the dash) |
| SABnzbd  | `GET {url}/sabnzbd/api?mode={mode}&output=json&apikey={apiKey}`  | query param only                   |
| Seerr    | `GET {url}/api/v1/{endpoint}`                                    | `X-Api-Key` header                 |
| Jellyfin | `GET {url}/{endpoint}`                                           | `Authorization: MediaBrowser Token="{apiKey}"` |

Three traps, each returning something that *looks* like an auth failure but isn't:

1. **`UrlBase` is not optional.** Sonarr (`/sonarr`) and Radarr (`/radarr`) set one here; Prowlarr and
   Bazarr do not. Omitting it returns the SPA's HTML index with a **200**, which reads as a broken
   key. Re-read with
   `grep -oP '(?<=<UrlBase>)[^<]*' ${CONFIG_ROOT}/config/{sonarr,radarr,prowlarr}/config.xml`.
2. **Prowlarr is `/api/v1/`,** not the `/api/v3/` Sonarr and Radarr use. v3 on Prowlarr 404s.
3. **Bazarr's header is `X-API-KEY`**, not `X-Api-Key`.

Prefer the header over `?apikey=` on the *arrs — the query parameter is rejected with 401 on these
versions. SABnzbd is the reverse: query parameter only.

Keys can be re-read from disk rather than each web UI:

```
${CONFIG_ROOT}/config/{sonarr,radarr,prowlarr}/config.xml   <ApiKey>
${CONFIG_ROOT}/config/bazarr/config/config.yaml             auth: → apikey:  (the first one; later
                                                            entries are Bazarr's copies of the
                                                            sonarr/radarr/jellyfin keys)
${CONFIG_ROOT}/config/sabnzbd/sabnzbd.ini                   api_key =
${CONFIG_ROOT}/config/jellyseerr/settings.json              main.apiKey
```

The `seerr` container mounts `config/jellyseerr`; any `overseerr/` or `jellyseerr.bak/` siblings are
stale leftovers.

**Jellyfin differs**: no `UrlBase`, and its key is created at Dashboard → API Keys and shown *once* —
there is no way to read it back. The older `X-Emby-Token: {apiKey}` header still works on 10.11 and
passes through curl more easily. Useful endpoints: `/System/Info`, `/Users`,
`/Library/VirtualFolders`, `/Items`. Library settings are written with
`POST /Library/VirtualFolders/LibraryOptions` — send the **full** `LibraryOptions` object read from
`/Library/VirtualFolders`, or unsent fields reset to defaults. Verify a key with `GET /Users`; on
10.11.11 `GET /Users/Me` returns **400** even with a valid key, so it is useless as a health check.

## Operational Traps

Each of these cost real time to find.

**Recyclarr instance names must be globally unique across `sonarr:` AND `radarr:`.** Duplicate names
make v8 abort at config load with only a `DBG`-level `Duplicate instances:` line — no `[ERR]`,
`[WRN]` or `[FTL]`, and the cron wrapper still prints `job succeeded`. Nothing syncs, silently.
Per-service runs (`sync sonarr`) scope to one block and never hit it, so it looks fine when you set it
up. Verify with `docker exec recyclarr recyclarr sync --preview` — it must print **both** servers.

**Quality profiles are referenced by collections, not just movies.** Radarr refuses to delete a
profile no movie uses because *collections* still point at it. Check `/api/v3/collection` as well as
`/api/v3/movie`. Each arr should have exactly one profile — extras have zero custom-format scores and
are an open door for a TrueHD/PGS remux.

**Hardlinks work on Windows drives mounted into WSL** (9p/drvfs), which people assume breaks `link()`.
Verify with a direct test across `downloads/completed → complete/tv`: same inode, link count 2. Files
showing `nlink=1` *after* import is expected, because the arrs remove the source. Do not "fix" it.

**Your Usenet provider's real connection cap may be below the advertised one.** Exceeding it produces
sustained `502 Too many connections` rather than a clean error. Change the value only when the plan
demonstrably changes, and watch SABnzbd's log afterwards.

**`Language: Not English` fires on "no English audio present"**, true of the original cut of any
foreign-language film. Scored `-500` (soft preference), *not* `-10000`, so English wins when it
exists but Parasite is still grabbable. Dual-audio dubs are blocked separately by `German DL` and
`MULTi` at `-10000`. `Dubs Only` is deliberately unscored — it would block English anime dubs.

**Jellyfin users need an explicit audio language.** Set `AudioLanguagePreference='eng'` and
**`PlayDefaultAudioTrack=False`** on every user. Leaving the latter true means "trust the mux", and
muxes lie: one NORDiC release flagged all six audio tracks `default=1`, resolving to the first
(Danish); a `German.DL` release had German as stream 1 with `default=1`.

**`_UNPACK_` / `_FAILED_` prefixes match `downloadClientWorkingFolders`**, so Radarr refuses to import
from those folders — "File is still being unpacked". Rename the folder, then
`GET /manualimport?folder=…` and `POST /command {name:ManualImport}`. Wanted films can sit there for
weeks looking like residue.

**Sonarr's `RenameSeries` renames files only, not the series folder.** To apply `seriesFolderFormat`
to existing series: `PUT /series/editor {seriesIds, rootFolderPath:'/data/complete/tv', moveFiles:true}`.

**Seerr never marks a TV request available from Jellyfin — only the Sonarr Scan can.** Its 5-minute
recently-added scan reads `GET /Items/Latest`, which returns `Type=Episode` for a TV library, and
`processItem()` handles `Movie`/`Series` only and drops the rest with no log line. The signature is a
`Beginning to process recently added for library: TV Shows` immediately followed by
`Recently Added Scan Complete` with no per-title line, while Movies logs one. Movies flip in 60s; TV
sits at "Processing" until the daily full scan. Fix is the **Sonarr Scan at `0 */5 * * * *`**, which
derives availability from Sonarr alone and completes the request in the same write. Unfixed upstream
in both projects and unaffected by Jellyfin version — do not go looking for an upgrade. Full
reasoning, the brand-new-series hierarchy bug it also sidesteps, and the library-size ceiling are in
[docs/seerr.md](./docs/seerr.md).

**Seerr's `apiRequestTimeout` is 45000 here, and it is a palliative, not a fix.** The Download
Tracker POSTs `RefreshMonitoredDownloads` — a database *write* — to both arrs every minute before
reading the queue, so it blocks whenever an arr's own work holds the SQLite write lock, and the
requester sees no download progress. 45000 rather than 30000 because Sonarr 4.0.19 returns HTTP 500
at 30.07-30.10s under a held lock, which a 30000 client deadline races by ~75ms (Seerr aborts first
and logs a *timeout*, disguising the 500 as a network fault) and because Radarr has no such ceiling.
**But 45000 does not hold under real load:** sampled during an active season-pack download,
`POST /command` reached **66.35s** on Radarr and **31.36s on Sonarr with a 201** — past the supposed
ceiling, so treat that 30s figure as per-attempt behaviour under a synthetic lock, not a law. Three
consecutive live cycles failed at exactly 45.01s during that download. **The finding that matters:
`GET /queue` never exceeded 9ms in any test** — synthetic lock held, real download in flight, either
arr. Only the write Seerr does not need ever blocks, so no timeout value is the right answer and the
tail is lost exactly when a requester is watching. Both arrs are already `journal_mode=wal`, so
that is not tunable either. Measurements and why patching `downloadtracker.js` in the container is
itself a trap (image bumps wipe it silently) are in [docs/seerr.md](./docs/seerr.md).

**Bazarr provider reality (1.6.0):** `podnapisi` was removed upstream and is silently dropped from
`enabled_providers` on restart; `subsource` needs an API key and throttles with `ConfigurationError`
without one; `subf2m` needs a `user_agent` or fails identically, forever. Enabled and healthy here:
`opensubtitlescom`, `gestdown` (TV only), `subf2m`, `yifysubtitles`. Subscene, which most guides still
recommend, shut down in 2024.

**Intro Skipper's manifest URLs 308-redirect to a bare GitHub org** and cannot be added.
**Do NOT install TheIntroDB as a substitute** — its `Dispose()` throws `ObjectDisposedException: The
CancellationTokenSource has been disposed`, taking down Jellyfin's shutdown path so the server
**does not restart on its own**. It also produced zero segments across 206 lookups. Intro/outro
skipping comes from **Chapter Segments Provider** only, wherever the release has real chapter names.
See [docs/jellyfin-plugins.md](./docs/jellyfin-plugins.md).

**Removing a media-segment provider purges the segments table.** Re-run the **Media Segment Scan**
task afterwards or every skip button silently disappears.

**The iPhone app's "Use native video player (beta)" breaks all playback.** It swaps bundled VLCKit
for iOS AVPlayer, which cannot open MKV — so Jellyfin remuxes to a *live-written* fragmented MP4 with
no known duration and empty `major_brand`/`compatible_brands`. AVFoundation rejects that with
`-11850 AVErrorServerIncorrectlyConfigured` on **every** file. Not a server misconfiguration; leave
the toggle off rather than hunting the server for it.

**"Runtime of all episodes is 0, unable to validate size until it is available"** is a *TVDB metadata
gap*, not a scoring problem. Sonarr's quality definitions are MB-per-minute (`minSize: 15` for
WEBDL/WEBRip-1080p), so `runtime: 0` makes the size check uncomputable and **every** release is
rejected — even ones scoring 2275.

- `runtime` is **provider-managed**: `PUT /series/{id}` is silently ignored, and `RefreshSeries` won't
  help if TVDB itself has no runtime. Verify with `GET /series/lookup?term=tvdb:<id>`.
- **Workaround — force the grab**, which is what the UI's download button on a rejected release does:
  ```bash
  curl -s -X POST "$SONARR/api/v3/release" -H "X-Api-Key: $KEY" \
    -H 'Content-Type: application/json' -d '{"guid":"<guid>","indexerId":<id>}'
  ```
- **Permanent fix** is adding the runtime to TheTVDB. Do *not* drop `minSize` to 0 — recyclarr
  reverts it on the next sync anyway, and it would disable size validation for every series to work
  around one.

**Seerr's recently-added scan is a permanent no-op for TV libraries.** `getRecentlyAdded()` calls
`GET /Items/Latest`, which on Jellyfin 10.11 returns items of type **`Episode`** for a show library
(true with `GroupItems` true, false, or unset). Seerr's `processItem()` dispatches on **`Movie` or
`Series` only** and drops anything else with no log line and no error. So the 5-minute scan can never
mark a TV request available; only the daily `jellyfin-full-scan` can, because `getLibraryContents()`
asks for `IncludeItemTypes=Series,Movie,Others`. Movies are unaffected, which makes this look like "TV
is just slow" rather than a defect. Log signature: `Beginning to process recently added for library:
<TV lib>` followed immediately by `Recently Added Scan Complete` with **no per-title line**, while the
movie library logs one every cycle. Verified against Jellyseerr 3.4.1 / Jellyfin 10.11.11.

**A connector-driven Jellyfin refresh creates the items but not the series hierarchy.** After a
`POST /Library/Media/Updated` (what the arrs' Emby/Jellyfin connector sends) the Series, Season and
Episode items exist and are listable via `/Items?ParentId=<library>&Recursive=true` — but
`GET /Shows/{seriesId}/Seasons` and `GET /Shows/{seriesId}/Episodes` both return
`TotalRecordCount: 0`, with or without `userId`. Those two endpoints are exactly what Seerr counts
episodes with, so its full scan computes **zero** available episodes and **regresses** the media record
from status 3 (processing) to status 1 (unknown) — worse than not scanning. **This is also
user-visible in every Jellyfin client:** the web client's `itemDetails` page calls
`getSeasons(seriesId)` → `/Shows/{id}/Seasons` to render the series detail page, so a stranded series
shows poster art but no seasons or episodes to play. **Season packs are the common trigger** — all
episodes import in one connector fire with no subsequent fire to heal the stale
`SeriesPresentationUniqueKey`. Individual episode imports self-heal in 5–15 min because later
connector fires update the children's keys. Since Sonarr prefers season packs, this is the common
case. Worst-case resolution ≈36 h without intervention. The repair is
`POST /Items/{seriesId}/Refresh?metadataRefreshMode=FullRefresh`, measured at ~20 s. When debugging
"the episode is in Jellyfin but Seerr says Processing", check the `/Shows/...` endpoints — `/Items`
will happily tell you everything is fine.

**Wired up as of `scripts/install-jellyfin-refresh.sh`** — a Sonarr Custom Script connector on
*On Import Complete* that detects the stranding and repairs it automatically. See
[docs/sonarr.md](./docs/sonarr.md). Two corrections it carries, both of which were repeated as
fact in this file until they were checked against the running server:

- **There is no `recursive` parameter on `POST /Items/{id}/Refresh`.** Jellyfin 10.11's own
  OpenAPI spec gives it exactly `metadataRefreshMode`, `imageRefreshMode`, `replaceAllMetadata`,
  `replaceAllImages`, `regenerateTrickplay`. `Recursive=true` — which
  [jellyfin#17293](https://github.com/jellyfin/jellyfin/issues/17293) and every guide recommend —
  is silently dropped by ASP.NET Core. What *is* established: the flag cannot be doing anything,
  and `metadataRefreshMode=FullRefresh` on the series does heal the children. The mechanism is
  presumably a parent-to-child cascade, but that part is inferred from the behaviour, not read out
  of Jellyfin's source — treat it as unconfirmed.
- **A full library scan is not a reliable repair.** A stranded series was observed surviving
  **two consecutive** `jellyfin-full-scan` runs unchanged, then healing ~20 s after a single
  `FullRefresh`. Do not reach for `POST /Library/Refresh` as the heavier-but-equivalent option; it
  is not equivalent.

**SABnzbd's temp-folder free space turning red does not mean the disk is low.** `glitter.main.js`
colours it whenever the remaining queue exceeds free temp space (`mbleft/1024 > diskspace1`), which is
normal during a backfill and self-clears. `download_free = 50G` + `fulldisk_autoresume = 1` are the
real protection.

## Image Pinning

Images are pinned by tag + SHA256 digest, bumped by Renovate. Update both when changing one.

**`seerr` is the one service that is built, not pulled.** Its pin lives in the `FROM` of
`images/seerr/Dockerfile` instead of an `image:` key, and Renovate bumps it there the same way. The
build deletes the redundant `RefreshMonitoredDownloads` write from Seerr's download tracker, which is
what made download progress vanish during the very download being waited on. Do not "simplify" this
back to `image:` — see `images/seerr/README.md` for the measurements and for why a bump cannot
silently revert it. If a bump makes `docker compose up -d` fail in the patch script, that is the
design working: read the new `downloadtracker.js` and update the `sed`, don't bypass it.

**Update this stack with a plain `docker compose up -d`.** `--pull always` and `--no-build` both skip
the build silently (exit 0, no warning) and keep the old image, which leaves `seerr` on a stale base
with everything reporting healthy. Nothing asserts the patch at runtime — Seerr's healthcheck passes
identically on an unpatched image.

`prowlarr` is deliberately on a `-nightly` tag — moving to stable is a **downgrade across a database
migration**, which Prowlarr does not support and which needs a restore from
`${CONFIG_ROOT}/config/prowlarr/Backups/`, not just an image swap.
