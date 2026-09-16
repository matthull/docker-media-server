# Docker Media Server Guides

Setup guides for each service in the stack. See the [README](https://github.com/matthull/docker-media-server#readme) for architecture, installation, and configuration.

## Core Services

- [Seerr](seerr.md) — media request portal
- [Sonarr](sonarr.md) — TV show management
- [Radarr](radarr.md) — movie management
- [SABnzbd](sabnzbd.md) — usenet downloader
- [Bazarr](bazarr.md) — automatic subtitles
- [Prowlarr](prowlarr.md) — indexer management
- [Recyclarr](recyclarr.md) — quality profile sync
- [Uptime Kuma](uptime-kuma.md) — monitoring and alerting

## Guides

- [Tailscale / Remote Access](tailscale.md) — VPN setup for accessing services from anywhere
- [Cloudflare Tunnel](cloudflared.md) — public access without port forwarding
- [Jellyfin Library Updates](jellyfin-library-updates.md) — getting imports into Jellyfin, and why the first one silently doesn't
- [Jellyfin Plugins](jellyfin-plugins.md) — what's worth installing, and what breaks the server
- [Notifications](notifications.md) — ntfy alerting across the stack, and what not to send
- [Stack Watch](stack-watch.md) — alerts for dead containers, an offline host and requests that never download
- [Backups](backups.md) — local tarballs, plus off-site config backups with restic
- [Usenet Indexers](usenet-indexers.md) — recommended indexers for Prowlarr
