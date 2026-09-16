# Sonarr

TV management. Monitors for episodes, sends NZBs to SABnzbd, organizes the results.

**Web UI:** `http://<host>:8989`

## First-Time Setup

1. **Download Client** — Settings > Download Clients > Add SABnzbd. Host `sabnzbd`, port `8080`, plus its API key.
2. **Root Folder** — Settings > Media Management. `/data/complete/tv` if you use
   `docker-compose.override.yml`, otherwise `/tv`.
3. **Indexers** — sync automatically from Prowlarr; otherwise add them under Settings > Indexers.
4. **Quality Profiles** — let Recyclarr sync TRaSH profiles rather than hand-tuning.
5. **Tell Jellyfin about imports** — library placeholders plus the Emby / Jellyfin connector,
   *before* the first import. See [Jellyfin Library Updates](jellyfin-library-updates.md); without it
   the first series can stay invisible in Jellyfin.

## Things to Know

- **Hardlinks need one mount, not two.** The base compose maps `/tv` and `/downloads` separately,
  which Docker presents as two filesystems — `link()` fails and every import becomes a full copy.
  `docker-compose.override.yml.example` mounts `${MEDIA_ROOT}` once as `/data` to fix this. Copy it
  before importing anything, since switching later means re-pointing root folders and remote path
  mappings.
- **Root folder is not the downloads folder.** Root is where organized media lives.
- **`RenameSeries` renames files only, not the series folder.** To apply `seriesFolderFormat` to
  existing series: `PUT /series/editor {seriesIds, rootFolderPath, moveFiles:true}`.
- **Remote path mapping** is unnecessary here — Sonarr and SABnzbd see identical paths.

## Brand-new series arrive in Jellyfin with nothing to play

A series Jellyfin has never seen before lands with its poster and its overview and **no seasons
and no episodes** — `GET /Shows/{id}/Seasons` and `/Shows/{id}/Episodes` both return 0 while a
flat `GET /Items?Recursive=true` happily lists the episodes with the right `SeriesId`. Every
client renders the series page from those two endpoints, so the show looks present and is
unplayable. Seerr counts episodes with them too, so the request also stays "Processing".

The cause is [jellyfin#16097](https://github.com/jellyfin/jellyfin/issues/16097): the join runs on
`SeriesPresentationUniqueKey`, which is rewritten on the series once provider identification
finishes but left stale on any child created before that moment.

**Season packs are the case that does not recover.** Every new series strands briefly. Episodes
that arrive in several batches heal themselves within 5–15 minutes, because each import fires the
Jellyfin connector again and each fire rewrites the children's keys. A season pack imports every
episode in one batch, so no second fire ever comes. Sonarr prefers season packs, which makes the
unrecoverable case the common one — about 36 hours, until a full library scan happens to fix it.

**A full library scan is not a reliable repair** — measured here, a stranded series survived two
consecutive Jellyfin full scans untouched. A metadata `FullRefresh` on the series item fixed the
same series in about 20 seconds.

### The fix

```bash
./scripts/install-jellyfin-refresh.sh            # install or update
./scripts/install-jellyfin-refresh.sh --check    # has the deployed copy drifted?
./scripts/install-jellyfin-refresh.sh --uninstall
```

This copies `scripts/jellyfin-refresh-series.sh` into Sonarr and registers it as a Custom Script
Connect notification on **On Import Complete**. On each import it waits for provider
identification, asks Jellyfin how many seasons it can join to the series, and refreshes only if
the answer is zero — then confirms the seasons actually appeared, rather than trusting the 204.

Prerequisites, both in `.env`:

| Variable | What it is |
| --- | --- |
| `JELLYFIN_ARR_API_KEY` | Jellyfin API key. The same one the "Jellyfin library update" connector uses. |
| `JELLYFIN_INTERNAL_URL` | Optional. Jellyfin's address **from inside a container**; defaults to `http://host.docker.internal:8096`. |

Run `docker compose up -d sonarr` after setting them — a plain `restart` does not re-read `.env`.

**`JELLYFIN_INTERNAL_URL` is deliberately not the same setting as `JELLYFIN_URL`.** That one is
Jellyfin's address from the *host* and is what the stack watch uses; its `localhost:8096` default
is correct there and meaningless inside a container.

### Two traps that produce a call returning 204 and doing nothing

1. **There is no `recursive` parameter.** Jellyfin 10.11's own OpenAPI spec
   (`/api-docs/openapi.json`) gives `POST /Items/{id}/Refresh` exactly five query parameters:
   `metadataRefreshMode`, `imageRefreshMode`, `replaceAllMetadata`, `replaceAllImages` and
   `regenerateTrickplay`. Guides — including
   [jellyfin#17293](https://github.com/jellyfin/jellyfin/issues/17293) — tell you to send
   `Recursive=true`, and ASP.NET Core silently drops unknown query parameters, so that call is
   really just a `FullRefresh` — the flag cannot be doing anything. What is established by
   measurement: `metadataRefreshMode=FullRefresh` on the *series* heals its children, and
   `Default` does not. The mechanism is presumably a parent-to-child cascade, but that is inferred
   from the behaviour rather than read out of Jellyfin's source, so do not repeat it as fact.
2. **Refreshing too early achieves nothing**, because it re-reads the stale key. Hence the
   `REFRESH_DELAY` (60s by default) before the script looks at anything.

### Operating it

The connector runs detached — Sonarr executes custom scripts synchronously, and this one
deliberately sleeps — so its output is in a log inside the container, not in Sonarr's:

```bash
docker exec sonarr cat /config/jellyfin-refresh/jellyfin-refresh.log
```

Sonarr logs custom-script output only at **debug** level, so at the default level a broken key is
silent. `install-jellyfin-refresh.sh` runs the script itself during install for exactly that
reason, and prints the failure Sonarr would have swallowed.

When the worker cannot find the series, the log line names the cause it saw, because each one
sends you somewhere different:

| Log says | Meaning | Look at |
| --- | --- | --- |
| **could not reach Jellyfin** | no response at all | is Jellyfin up; `JELLYFIN_INTERNAL_URL` |
| **failed partway** | a 200 started, then timed out or dropped | Jellyfin overloaded or restarting |
| **refused … check the API key** | 401 / 403 | `JELLYFIN_ARR_API_KEY`, then `docker compose up -d sonarr` |
| **got HTTP 404** / **not a series list** | something answered that is not Jellyfin's API | `JELLYFIN_INTERNAL_URL` (address or base path) |
| **last answer was HTTP …** | any other error, e.g. 503 | usually Jellyfin still starting |
| **is not in Jellyfin** | a complete series list came back without it | is the series in a library Jellyfin watches |

Only the last row means Jellyfin actually searched its library. Likewise, after a refresh,
**STILL STRANDED** means Jellyfin answered and still shows no seasons, while **could not read the
season count after the refresh** means nobody knows whether it healed.

`REFRESH_DELAY`, `REFRESH_TIMEOUT`, `LOOKUP_TIMEOUT` and `REFRESH_DRYRUN=1` (detect and log, change
nothing) can be set in sonarr's environment to tune or observe it.

**The deployed copy lives in Sonarr's config volume, which is not in git.** It has to be there:
`docker-compose.override.yml.example` replaces sonarr's whole `volumes:` list, so a bind mount
added in `docker-compose.yml` would silently vanish on every host that wants hardlinks, and
`/config` is the one path both spellings share. Re-run the installer after a `git pull`, and use
`--check` to see whether the deployed copy has drifted.

## Links

- [Servarr Wiki](https://wiki.servarr.com/sonarr) · [Quick Start](https://wiki.servarr.com/sonarr/quick-start-guide)
- [TRaSH Guides — Sonarr](https://trash-guides.info/Sonarr/)
- [LinuxServer.io Docker](https://docs.linuxserver.io/images/docker-sonarr)
