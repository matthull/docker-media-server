# Stack watch

The services' own [notifications](Notifications) cover what each one can see about itself: a failed
download, an indexer health error, a request Seerr couldn't hand off. Some failures can't report
themselves:

- a container that has stopped or turned unhealthy, since it can't send its own alert;
- Jellyfin not answering, since it runs outside the stack;
- the host asleep, powered off or offline, since nothing on it can send anything;
- a request Sonarr or Radarr accepted and then waits on forever, because no release ever turns up.
  Seerr has no event for this, so it just sits there.

`monitoring/stack_watch.py` covers all four. It sends to the same ntfy topic as everything else.
It is one standard-library Python script run by two systemd user timers, with no new container and no
new account.

## What it sends

| Notification | When | Priority |
| ------------ | ---- | -------- |
| **Media stack on *host*: N problems** | A Compose service is missing, exited, restarting or unhealthy; Jellyfin's `/health` isn't `Healthy`; Docker isn't answering; or the Compose file can't be read. Only after the problem shows on two checks in a row. | High |
| **… all clear** | Everything in the alert above has recovered. It *replaces* the alert rather than adding a second notification. | Low |
| ***host* is unreachable** | The host hasn't checked in for `HEARTBEAT_GRACE`. ntfy.sh sends this, not the host. | High |
| ***host* is back online** | The first check after the unreachable alert fired. Says how long it was gone. | Default |
| **N wanted titles not downloaded after 3 days** | Daily. Monitored movies or episodes with no file and nothing in the download queue, `STALL_DAYS` after they were added or released, whichever is later. Re-sent when a new title joins the list, and as a reminder every `STALL_REMIND_DAYS`, at most twice per title. | Default |
| **Stack watch on *host* is failing** | The script itself failed two runs in a row, so its alerts are off. Replaced by "working again" when it recovers. | High |

Each kind of alert updates one notification in place (ntfy's `sequence_id`), so a problem that lasts a
day is one notification that changes, not ninety-six.

## How the host-offline alert works

A host can't report that it's gone, so it has to be reported from somewhere else. ntfy's scheduled
messages do that without a second service. Every check publishes an "unreachable" message delayed by
`HEARTBEAT_GRACE`, and each new one [replaces the one still
waiting](https://docs.ntfy.sh/publish/#updating-scheduled-notifications) because they share a sequence ID. While
the host keeps checking in, the delivery time keeps moving forward and nothing arrives. When the checks stop,
the last scheduled message is delivered.

ntfy.sh allows an anonymous IP **250 messages a day**, shared with every service on the host that
publishes. Re-scheduling every 15 minutes would use 96 of them. For grace periods of a day or more, the
script re-schedules only when the delivery time would move by an hour or more. The alert can then arrive
up to an hour *before* the grace period has fully passed since the last check-in. Shorter grace periods
re-schedule proportionally more often.

Setting `HEARTBEAT_GRACE=off` cancels a message that is already scheduled, and so does uninstalling.
Stopping the timers by hand does **not** cancel it; to do that, run
`python3 monitoring/stack_watch.py disarm`.

**Pick the grace for how the host is actually used.** On an always-on server, `1h` catches a crash
quickly. A laptop that sleeps overnight will alert every night at `1h`. Pick something longer than
its normal sleeps, or turn it `off` and rely on the container and Jellyfin checks. Its limits are 10s
to 3 days (ntfy's scheduling limit).

## Setup

1. Set up the ntfy topic first ([Notifications](Notifications), step 1), and subscribe to it on your phone.
2. Add the topic to `.env`. The rest is optional; `.env.example` documents each setting.

   | Variable | Default | Meaning |
   | -------- | ------- | ------- |
   | `NTFY_TOPIC` | required | Topic to publish to |
   | `HEARTBEAT_GRACE` | `24h` | Silence before "unreachable": `10s` to `3d`, or `off` |
   | `STALL_DAYS` | `3` | Days a wanted title may wait before the digest lists it |
   | `STALL_REMIND_DAYS` | `7` | Days between reminders about the same titles |
   | `JELLYFIN_URL` | `http://localhost:8096` | Jellyfin from this host, or `off` |

   Blank values use the default; only `off` turns a check off. API keys are read from each service's
   `config.xml` under `CONFIG_ROOT`, so there is nothing else to configure.

3. Install, as the user that runs Docker (no sudo):

   ```bash
   ./monitoring/install-stack-watch.sh
   ```

   It runs each check once by hand, and stops without enabling anything if one fails. Then it enables
   `media-stack-watch.timer` (every 15 minutes) and `media-stack-stalled.timer` (daily at 10:00). Both have
   `Persistent=true`, so a run that was due while the machine slept happens when it wakes.

   User timers only run while your user's systemd instance does. If the installer warns that linger is
   off, run `sudo loginctl enable-linger $USER`, or the watch stops whenever you log out.

To remove it: `./monitoring/install-stack-watch.sh --uninstall`.

Logs are in `journalctl --user -u 'media-stack-*'`, and state is in `~/.local/state/media-stack-watch/`.

## Testing

Force each alert once after installing. `--test` prefixes titles with `TEST:` and uses its own state
file and sequence IDs, so a forced alert never touches the real ones. `--dry-run` prints what would be
sent and saves nothing.

Put `HEARTBEAT_GRACE=off` on every `check --test` except the heartbeat test. Otherwise each one schedules a
`TEST:` unreachable alert that goes out a day later.

Run these from the repo root, one step at a time.

1. **A stopped container.** Pick one nobody watches, such as bazarr. The first check only notes the
   problem; the second sends "TEST: Media stack on *host*: 1 problem".

   ```bash
   docker stop bazarr
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   ```

   Then start it again. The next check sends "… all clear".

   ```bash
   docker start bazarr
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   ```

2. **Jellyfin not answering**, by pointing at a closed port twice, then one normal check for the all clear.

   ```bash
   JELLYFIN_URL=http://localhost:1 HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   JELLYFIN_URL=http://localhost:1 HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   ```

3. **Docker not answering**, the same way.

   ```bash
   DOCKER_HOST=unix:///nonexistent.sock HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   DOCKER_HOST=unix:///nonexistent.sock HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   ```

4. **Host unreachable.** Schedule a 30-second heartbeat and don't check in again:

   ```bash
   HEARTBEAT_GRACE=30s python3 monitoring/stack_watch.py check --test
   ```

   Wait a minute; "TEST: *host* is unreachable" arrives. Then check in, which sends "back online" and
   schedules another test alert, and cancel that one **immediately** (within 30 seconds):

   ```bash
   HEARTBEAT_GRACE=30s python3 monitoring/stack_watch.py check --test
   python3 monitoring/stack_watch.py disarm --test
   ```

5. **Stalled digest**, treating everything wanted as overdue:

   ```bash
   STALL_DAYS=0 python3 monitoring/stack_watch.py stalled --test
   ```

To see what the topic received, poll the cache with
`curl -s "https://ntfy.sh/<topic>/json?poll=1&since=10m"`.
Add `&scheduled=1` to also list messages still waiting to be sent.

## Not covered

- **A download stuck in the queue.** Anything in the queue counts as progress, including a download that
  has stalled at 0 B/s. SABnzbd's failure alert and the *arrs' "manual interaction required" alert cover a
  download that fails or can't be imported, but not one that never finishes.
- **ntfy.sh itself.** Every alert here, and the offline alert especially, depends on ntfy.sh being up.
- **A container stuck in `health: starting`** counts as fine. Docker normally turns that into
  `unhealthy` once its retries run out.
