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
shared `catch`, and Seerr logs `Unable to get queue from <arr> server` — exactly while someone is
watching the thing they just requested, because the contention is caused by that very download.

**What the requester actually sees is a frozen progress bar, not a missing one.** The `catch` only
logs: `this.radarrServers[server.id]` is assigned solely on success and is never cleared on failure,
and `resetDownloadTracker()` is a separate job on `0 0 1 * * *` (daily at 01:00). So a failed cycle
leaves the *previous* snapshot in place and the card keeps showing a stale percentage and a
stale ETA until a cycle succeeds. The bar is genuinely absent only for a download whose very first
tracker cycles all failed. Earlier notes in this repo said progress "disappears"; that was wrong,
and arguably understated the problem — a confidently wrong ETA is worse than a missing one.

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

### The one way to get a stale base anyway

**`docker compose up -d --pull always` and `--no-build` both skip the build silently.** Verified: with
a changed `FROM`, `up -d --pull always` printed only `Container … Running`, exit 0, no build, no
recreate, no warning, and the container kept running the old base. `--no-build` likewise succeeded
while the patch script was rigged to fail.

This cannot produce an *unpatched* Seerr — the image reused is the patched one — but it can leave the
base stale indefinitely with everything reporting healthy. It matters because
`docker compose pull && docker compose up -d --pull always` is a common stock update recipe.

**Update this stack with a plain `docker compose up -d`.** Nothing in the image asserts the patch at
runtime: Seerr's healthcheck (`/api/v1/status`) passes identically on an unpatched image. The check
that closes this is `docker exec seerr grep -c refreshMonitoredDownloads /app/dist/lib/downloadtracker.js`
returning 0, which belongs in `monitoring/stack_watch.py` and is not there yet.

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

| Control                                          | Result                                                     |
| ------------------------------------------------ | ---------------------------------------------------------- |
| Base with the call sites already gone             | build failed: `expected exactly 2 ... found 0`              |
| Base with the calls moved behind `this.`          | build failed: `2 reference(s) survived the patch`           |
| Calls moved under a braceless `if`, expression statement following | build failed: `not both immediately followed by the getQueue they guard` |
| Real base                                         | build succeeded; diff is exactly the two deleted lines      |

The third control came out of a fresh-context review and is the reason the pair check exists. The
original guards only proved the calls were *gone*, not that removing them was *safe*: a refresh moved
under a braceless `if` passed `before=2`, `after=0`, `removed=2` and `node --check` while silently
re-binding the next statement into the `if` body. That fixture is valid JavaScript, so nothing would
have complained. Today's file happens to be protected because a `const` declaration follows (which
`node --check` does reject there) — protection by luck, not by design. The pair check requires each
refresh to be immediately followed by the `const queueItems = await <arr>.getQueue();` it precedes.

## What is not established

- **No browser observation.** The tracker was seen reading a real non-empty queue, and
  `GET /api/v1/request` returned the populated `media.downloadStatus` a request card renders from
  (release name, `status=downloading`, size, `timeLeft`, ETA), for a real download that then went
  through to Available. Nobody has watched the bar itself, and the one sample was caught at ~100%,
  so a *changing* percentage is still unobserved.
- **Two unexplained observations, both probably measurement contamination.** In an 18-minute window
  after the change, one cycle failed with all four errors landing inside 60 ms on both arrs after a
  2.5-minute gap in which no scheduled job fired at all; and one Download Sync took 25 s to log its
  queue result, on a read measured elsewhere at ≤9 ms. The Docker daemon on the host was visibly
  stalled across that window (an unrelated `compose up -d` hung ~5 minutes on an `alpine` container
  running `sleep`) while image builds ran concurrently, which fits a starved Node event loop far
  better than anything in this code path. **It was not reproduced and host load was not sampled.**
  If either recurs on an idle host, it is a real defect unrelated to the removed write — and the
  25 s figure would contradict "`GET /queue` never blocks", which is worth chasing on its own.

## Rolling this back

`git revert` the commit that added this directory, then `docker compose up -d seerr`. That restores
the upstream `image:` pin; the only thing that returns with it is the blocking write, so expect
`Unable to get queue` errors to reappear during downloads.

## When a bump breaks the build

That is the design working. Read the new `downloadtracker.js`, confirm the call sites still exist in
some form, and update the `sed` expression — or, if upstream has finally fixed the defect, delete
this directory and put `image:` back on the service in `docker-compose.yml`.
