# Jellyfin Library Updates

How an import from Sonarr or Radarr reaches Jellyfin, and the three ways an empty library folder
makes that fail silently — which is where every fresh install starts.

Jellyfin is not in this stack, so nothing here tells it a file arrived unless you set it up. There
are two routes, and both end in the same place:

- **Real-time monitoring** — Jellyfin's own filesystem watcher on each library folder.
- **The "Emby / Jellyfin" connector** in Sonarr and Radarr, which calls
  `POST /Library/Media/Updated` with the folder it just imported into.

Each hands the path to the same debounced refresher, which waits `LibraryMonitorDelay` (60 s by
default) and then refreshes that part of the library. Measured on a test install, a new movie or episode appeared
**60–62 s** after import on either route.

Verified against Jellyfin 10.11.11, Sonarr 4.0.19 and Radarr 6.3.0. Source references are to
Jellyfin tag `v10.11.11`.

## Before the first import: give each library folder a placeholder

```bash
MEDIA_ROOT=/your/media/root    # the value from .env
mkdir -p "$MEDIA_ROOT/complete/tv" "$MEDIA_ROOT/complete/movies"
touch "$MEDIA_ROOT/complete/tv/.keep-library-nonempty" \
      "$MEDIA_ROOT/complete/movies/.keep-library-nonempty"
```

Do this **before the first `docker compose up`**: otherwise Docker has already created these folders
owned by root, and `touch` fails with "Permission denied" (fix with `sudo chown -R PUID:PGID` using
the values from `.env`). Type the real path for `MEDIA_ROOT`.

Then add the libraries in Jellyfin (Dashboard > Libraries): **Shows** at `<MEDIA_ROOT>/complete/tv`
and **Movies** at `<MEDIA_ROOT>/complete/movies`, as Jellyfin sees those folders, with **Enable
real-time monitoring** on. If the libraries already existed, run **Scan All Libraries** once after
creating the placeholders. Never delete these files.

This is recommended outright, not as an optional workaround. A fresh install starts with empty
library folders, and Jellyfin handles an empty library folder in three ways that all fail silently:

1. **An empty library folder is never watched.** `LibraryMonitor.Start()` only runs at a library
   scan, when a library is added or removed, and at startup. It skips any library whose folder has
   no entries: `Folder.IsLibraryFolderAccessible` →
   `DirectoryService.IsAccessible` is `GetFileSystemEntryPaths(path).Any()`. When content arrives
   later, no watch is added until the next `Start()`. Measured: a movie hardlinked into an empty
   library was still missing after 300 s, and an episode still missing after 180 s. Jellyfin's
   inotify watches covered neither folder.
2. **An empty library folder loses its root `Folder` item, and then the connector does nothing.**
   Any scan while the folder is empty logs `Removing item, Type: Folder, Name: movies`, even a scan
   triggered by adding or removing a *different* library. The connector's
   `POST /Library/Media/Updated` walks up from the path it was given to find an item to refresh
   (`FileRefresher.GetAffectedBaseItem`). It finds none and returns **with no log line and a
   204**. Measured: a movie plus a correctly mapped `Created` call was still missing after 180 s.
3. **Deleting the last item never removes it.** The refresh stops at
   `Library folder <path> is inaccessible or empty, skipping`. That guard exists so an unmounted
   drive doesn't wipe the library. Measured: still listed after 240 s.

Any entry at all makes the folder count as accessible, which fixes all three. With the placeholders
in place, a first movie into an otherwise empty library appeared in **60.3 s**, and deleting that
last movie removed it in **60.3 s**.

Why this file is safe to leave there:

- **Jellyfin ignores it.** `**/.*` is in `IgnorePatterns.cs`, so it is never indexed, and changing
  it never triggers a refresh.
- **Sonarr and Radarr ignore it.** It does not appear in `GET /api/v3/rootfolder`
  `unmappedFolders`.
- **It keeps the unmounted-drive guard.** The file lives on the media drive. If the drive is not
  mounted, the folder is empty again and Jellyfin still refuses to purge the library.
- **It travels with the drive, not with the repo.** Any new library root needs its own, including
  after a migration to a fresh disk.

Source: `Emby.Server.Implementations/IO/LibraryMonitor.cs`,
`MediaBrowser.Controller/Entities/Folder.cs`, `MediaBrowser.Controller/Providers/DirectoryService.cs`,
`Emby.Server.Implementations/IO/FileRefresher.cs`,
`Emby.Server.Implementations/Library/IgnorePatterns.cs`, in
[jellyfin/jellyfin](https://github.com/jellyfin/jellyfin/tree/v10.11.11).

## Also add the connector

With the placeholders in place, the watcher alone worked in every trial. Add the connector anyway,
because it does not depend on a watcher being armed. Every library scan disposes and re-creates the
watchers, and a watcher that hits an error is disposed and stays gone until the next `Start()`. The
`POST /Library/Media/Updated` route was measured working with real-time monitoring switched **off**
(60.4 s).

### 1. A dedicated API key

Jellyfin > Dashboard > **API Keys** > +, named for what uses it (e.g. `sonarr-radarr-library-update`).
It is shown once and cannot be read back, so put it in `.env` as `JELLYFIN_ARR_API_KEY`. Sonarr's
[series-refresh connector](Sonarr#brand-new-series-arrive-in-jellyfin-with-nothing-to-play) reads it
from there. A dedicated key can be revoked without
breaking Seerr or anything else.

### 2. Sonarr and Radarr: Settings > Connect > + > Emby / Jellyfin

| Field | Value |
| --- | --- |
| Name | anything, e.g. `Jellyfin library update` |
| Host | See [Reaching a Jellyfin that is not in this stack](#reaching-a-jellyfin-that-is-not-in-this-stack) |
| Port | `8096` |
| Use SSL | off, unless Jellyfin itself serves HTTPS |
| URL Base | empty, unless Jellyfin is configured with a base URL |
| API Key | the key from step 1 |
| Send Notifications | off (Emby only; not supported on Jellyfin) |
| Update Library | **on** — this is the whole point |
| Map Paths From / To | see the table below — type literal paths; the UI does not expand `${MEDIA_ROOT}` |

Triggers:

- **Sonarr:** On File Import, On File Upgrade, On Rename, On Series Delete, On Episode File Delete.
- **Radarr:** On File Import, On File Upgrade, On Rename, On Movie Delete, On Movie File Delete.

The arr sends the series or movie folder **as the arr sees it**, and Jellyfin looks that path up as
**Jellyfin** sees it. If the two differ and nothing maps them, Jellyfin finds nothing and does
nothing, silently. That is the second trap above again, from a different cause.

| Your layout | Map Paths From | Map Paths To |
| --- | --- | --- |
| `docker-compose.override.yml` (one `/data` mount) | `/data/` | Jellyfin's path to `${MEDIA_ROOT}`, with a trailing slash |
| Base compose only, Sonarr | `/tv/` | Jellyfin's path to `${MEDIA_ROOT}/complete/tv/` |
| Base compose only, Radarr | `/movies/` | Jellyfin's path to `${MEDIA_ROOT}/complete/movies/` |

For a Jellyfin installed on the host, "Jellyfin's path" is the host path, which is the value of
`MEDIA_ROOT` in `.env`. For a containerized Jellyfin, it is the path inside *that* container. The
`/data/` row is the one verified on a test install: the request body captured off the wire carried the host path.
The two base-compose rows follow from the same rule but were not measured. If a title doesn't
appear, check them first.
A Windows-native Jellyfin would need a Windows path in **Map Paths To**. That case has not been
tested.

### 3. Check the key yourself: the Test button does not

**The connector's Test button returns success with a wrong API key**, for saved and unsaved
connectors alike (`POST /api/v3/notification/test` → 200). Check the key directly:

```bash
docker exec sonarr sh -c 'curl -s --max-time 10 -o /dev/null -w "%{http_code}\n" \
  -H "X-Emby-Token: $JELLYFIN_API_KEY" "$JELLYFIN_URL/System/Info"'
```

`200` means the key and address work from inside the container. `401` means the key is wrong.
`000` means nothing answered: the address is wrong, or something on the host drops the traffic. This checks the key in `.env`, which the sonarr container
gets through `docker compose up -d`. It does not check the key pasted into the connector, so paste
the same one. Radarr's container has no such variable, so this checks its Host only if the two
connectors use the same address.

## Reaching a Jellyfin that is not in this stack

`docker-compose.yml` gives `sonarr`, `radarr` and `seerr` an
`extra_hosts: host.docker.internal:host-gateway` entry. Docker Engine on Linux does not define
`host.docker.internal` by default. Without that entry the name does not resolve inside the container.

| Where Jellyfin runs | Use as Host |
| --- | --- |
| Natively on the same Linux host | `host.docker.internal` |
| Natively on Windows, Docker Engine inside WSL | The Windows host's LAN IP. `host.docker.internal` is the WSL distro there, not Windows; see [Cloudflare Tunnel](Cloudflared#why-that-address) |
| In a container on the `sofa-squad` network | its container name |
| On another machine | its LAN IP (reserve it in your router's DHCP settings) |

`host-gateway` needs Docker Engine 20.10 or later. Rootless Docker and Podman have not been tested
with it.

Use the same host for `JELLYFIN_INTERNAL_URL` in `.env` — as a full URL, e.g.
`http://<LAN IP>:8096` — whenever it isn't `http://host.docker.internal:8096`. That name reaches the host through Docker's bridge, so anything on
the host that filters bridge traffic — a firewall, or a VPN client with its own kill switch — makes
the connector time out while Jellyfin still works from the LAN. The `docker exec` check in step 3 above
tells the cases apart.

## Did it work?

Jellyfin's log shows `<library> (<path>) will be refreshed.` about 60 s after an import. **That line
does not prove the connector fired**, because the watcher produces the same line and the two
routes merge in the same refresher. `POST /Library/Media/Updated` itself logs nothing. To confirm
the connector, set the arr's log level to Debug (Settings > General), import something, then look
in `logs/radarr.debug.txt` (or `sonarr.debug.txt`) under its config folder for:

```
MediaBrowser|Scheduling library update for movie 1
MediaBrowser|Performing library update for 1 movies
MediaBrowserProxy|... [POST] http://.../Library/Media/Updated: 204.NoContent
```

Set the level back afterwards. A 204 only means Jellyfin accepted the call; if the mapping is
wrong, Jellyfin still answers 204 and refreshes nothing. If the title isn't in Jellyfin about a
minute after that 204, check **Map Paths** first.

## What neither route covers

- **A library scan that starts inside the 60 s window drops the change.** A scan stops the monitor,
  and stopping it discards pending refreshes, for both routes. The scheduled **Scan Media Library**
  task (every 12 h by default) is the backstop. On a quiet server this is rare. If you script
  scans, keep them away from imports.
- **A brand-new series can arrive with no seasons or episodes to play.** That is a different
  Jellyfin bug, and it bites on season packs. Sonarr has a separate fix: see
  [Sonarr](Sonarr#brand-new-series-arrive-in-jellyfin-with-nothing-to-play).
- **A movie refresh may rescan the whole Movies library**
  ([jellyfin#16172](https://github.com/jellyfin/jellyfin/issues/16172)). This is harmless at small
  library sizes.
