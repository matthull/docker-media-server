# Derived Seerr image — no RefreshMonitoredDownloads write

Seerr is the one service in this stack that is **built, not pulled**. This directory is why.

## The defect

`downloadtracker.js` runs once a minute and, for each arr, does this:

```js
await radarr.refreshMonitoredDownloads();   // POST /api/v3/command
const queueItems = await radarr.getQueue(); // GET  /api/v3/queue
```

The `POST` inserts a row into the arr's `Commands` table, so it needs the arr's **SQLite write
lock** — the same lock the arr itself holds while it is grabbing or importing a release. The `GET`
that it guards needs no lock at all.

Measured on this host while a season pack was downloading:

| Call                  | Radarr     | Sonarr     |
| --------------------- | ---------- | ---------- |
| `POST /api/v3/command`| **66.35s** | **31.36s** |
| `GET /api/v3/queue`   | ≤ 9ms      | ≤ 9ms      |

`GET /queue` never exceeded 9ms in *any* measurement taken — synthetic lock held 6-60s, live
download, either arr. Only the write ever blocks.

When the `POST` outruns Seerr's `network.apiRequestTimeout` the whole cycle is abandoned in the
shared `catch`, Seerr logs `Unable to get queue from <arr> server`, and **download progress
disappears from the UI** — exactly while someone is watching the thing they just requested, because
the contention is caused by that very download.

Raising the timeout is a palliative, and it was tried first: at `45000`, a live 90-minute window
still lost **15 of 90 cycles (16.7%)**. The tail grows with the download, so no timeout value is the
right answer. Not issuing the write is.

## Why dropping it is safe

Both arrs already run the command themselves, on a schedule Seerr does not control:

```
sonarr: RefreshMonitoredDownloads  interval=1min  lastDuration=0.034s
radarr: RefreshMonitoredDownloads  interval=1min  lastDuration=0.028s
```

That is the *same* one-minute cadence as Seerr's own `Download Sync` job, so removing Seerr's copy
costs at most about a minute of queue staleness on a progress indicator — and in exchange the read
stops being gated behind a lock it never needed.

Upstream has no fix landing. [PR #3473](https://github.com/seerr-team/seerr/pull/3473) implements the
fuller version (await command completion with a deadline, coalesce concurrent syncs) and was **closed
as a duplicate** of [PR #1055](https://github.com/seerr-team/seerr/pull/1055), which is a one-line
`pageSize` change that does not touch this code path at all. Do not wait for either.

## Why it is a Dockerfile

The patch has to live inside the image, and this repo pins images by tag + digest with Renovate
automerging bumps. A bare `docker exec` edit would be reverted by the next bump with every container
still reporting healthy — the silent-degradation failure this stack is explicitly built to avoid.

The arrangement here cannot do that:

- **The patch is in git** and travels with the repo, so a migration or a fresh clone carries it.
- **`patch-downloadtracker.sh` re-runs on every build** and asserts what it found *before* and *after*
  editing. If the two call sites are not there in the expected shape, it exits non-zero, the build
  fails, **no image is produced**, and the previously built patched image keeps serving. The failure
  surfaces on the build host, in front of whoever is doing the bump.
- **`pull_policy: build`** in `docker-compose.yml` makes `docker compose up -d` rebuild from the
  pinned base rather than reuse the last local build, so a Renovate bump of the `FROM` actually
  reaches the running container instead of sitting unused. Layer caching makes the no-op case ~1s.
- **No `image:` key** on the service, so nothing can pull over the built image.

Both guard directions are tested, not assumed — see the negative controls below.

## Verifying

```sh
# the running container must have zero call sites
docker exec seerr grep -c refreshMonitoredDownloads /app/dist/lib/downloadtracker.js   # -> 0 (exit 1)

# and must still be serving
docker inspect seerr --format '{{.State.Health.Status}}'
```

The functional test is `../../local/seerr-timeout/cycletest.py HOLD CYCLES`, which holds both arr
write locks across a Download Sync boundary. **A clean run on an idle stack proves nothing** — the
pre-change baseline contains a 210-cycle clean run with the bug fully present. Induce contention or
measure during a real import.

## Negative controls (2026-09-16)

The build guards were proven to fail, not just to pass:

| Control                                        | Result                                                      |
| ---------------------------------------------- | ----------------------------------------------------------- |
| Base with the call sites already gone           | build failed: `expected exactly 2 ... found 0`               |
| Base with the calls moved behind `this.`        | build failed: `2 reference(s) survived the patch`            |
| Real base                                       | build succeeded; diff is exactly the two deleted lines       |

## When a bump breaks the build

That is the design working. Read the new `downloadtracker.js`, confirm the call sites still exist in
some form, and update the `sed` expression — or, if upstream has finally fixed the defect, delete
this directory and put `image:` back on the service in `docker-compose.yml`.
