# Seerr, with the redundant RefreshMonitoredDownloads write removed from its download
# tracker. See README.md in this directory for the measurements that motivate it, and
# patch-downloadtracker.sh for what is actually changed (two deleted lines).
#
# The base reference below is the only place the Seerr version is pinned. Renovate's
# dockerfile manager bumps it exactly as it bumps the `image:` lines in
# docker-compose.yml -- tag and digest together.
#
# A bump can never silently drop the patch: the patch script re-runs on every build and
# refuses to produce an image it could not patch.
FROM ghcr.io/seerr-team/seerr:v3.4.1@sha256:f4768de5f616248d723e05891f3345a1402123775d03bf0890dbfedc0831bda1

COPY patch-downloadtracker.sh /usr/local/share/patch-downloadtracker.sh

RUN sh /usr/local/share/patch-downloadtracker.sh
