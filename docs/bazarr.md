# Bazarr

Automatic subtitles. Connects to Sonarr/Radarr, scans the library, fetches what's missing.

**Web UI:** `http://<host>:6767`

## First-Time Setup

1. **Connect Sonarr & Radarr** — Settings > Sonarr / Radarr. Use container names (`sonarr:8989`,
   `radarr:7878`) and their API keys.
2. **Set Languages** — Settings > Languages. Create a profile and set a cutoff language so Bazarr
   stops searching once it has one.
3. **Enable Providers** — see below; the defaults suggested in most guides are dead.
4. **Path Mapping** — not needed here, the mounts match.

Bazarr's API key header is `X-API-KEY` — note the dashes, unlike the *arrs' `X-Api-Key`.

## Provider reality (1.6.0)

Most guides still recommend Subscene, which **shut down in May 2024**. Of what remains:

| Provider           | Status                                                              |
| ------------------ | ------------------------------------------------------------------- |
| `opensubtitlescom` | Works. Needs a free account.                                        |
| `gestdown`         | Works, TV only.                                                     |
| `subf2m`           | Works **only** with a `user_agent` set — otherwise it fails forever. TV matching is patchy, see below. |
| `yifysubtitles`    | Works, movies only.                                                 |
| `podnapisi`        | Removed upstream; silently dropped from `enabled_providers` on restart. |
| `subsource`        | Needs an API key; throttles with `ConfigurationError` without one.   |

**subf2m's user agent** is set at Settings > Providers > subf2m.co > "User-agent header", or with
`settings-subf2m-user_agent` via `POST /api/system/settings`. The UI asks for "a unique and credible
user agent". A current desktop-browser string has been seen working.
- **An empty one makes the provider refuse to start** (`ConfigurationError: User-agent config
  missing`). Bazarr then throttles it for 12 h, and the first search after that throttles it again,
  indefinitely. subf2m.co's search page answers the same with or without a user agent; the refusal
  is Bazarr's own.
- **Saving a user agent does not clear an existing throttle.** Reset it in the provider status view,
  or with `POST /api/providers` and `action=reset`. Otherwise subf2m stays throttled for up to 12 h
  after the fix.

**subf2m silently misses TV seasons whose listing it can't parse.** It accepts two title shapes:
- a word ordinal or "complete" between a ` - ` or ` (` separator and "Season"/"Series":
  `Show - First Season`, `Show (First Season)`, `Show - Complete Series`;
- or any title with a 1–2 digit number after a space: `Show - Season 1`, `Show Season 2`.

Listings like `Show   Season One (2015)` or `Show (Season 1)` match neither, and subf2m.co does use
such forms for some seasons. That season then gets nothing from subf2m and no error; there is only a
debug-level log line. The ordinal list also misspells 13th, 14th, 18th and 20th, so those never match
by word. Movie matching uses the listing's `(year)` instead and is unaffected by any of this. Treat
subf2m as a backup source for TV.

**`/api/providers` shows `Good` for every enabled provider that isn't throttled**, including one that
has never been queried. To confirm a provider really returns subtitles, run an interactive search:
`GET /api/providers/movies?radarrid=<id>` or `GET /api/providers/episodes?episodeid=<id>`.
- Both list candidates per provider and download nothing; only the `POST` form downloads.
- They are not quite side-effect free: they re-index that file's existing subtitles, and a provider
  that errors during the search gets throttled.

## Things to Know

- **Text subtitles only, deliberately.** Image subtitles (PGS/VOBSUB) force Jellyfin to burn them in,
  which triggers a full video transcode. Keeping a text subtitle available on everything is what
  avoids that.
- **Forced subtitles** — the ones for foreign-language dialogue. Configured separately.
- **First scan** starts automatically once Sonarr/Radarr are connected.

## Links

- [Bazarr Wiki](https://wiki.bazarr.media/) · [Setup Guide](https://wiki.bazarr.media/Getting-Started/Setup-Guide/)
- [LinuxServer.io Docker](https://docs.linuxserver.io/images/docker-bazarr)
