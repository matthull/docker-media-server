# Stack Watch

The services' own [notifications](Notifications) cover what each one can see about itself: a failed
download, an indexer health error, a request Seerr couldn't hand off. Some failures can't report
themselves:

- a container that has stopped or turned unhealthy, since it can't send its own alert;
- Jellyfin not answering, since it runs outside the stack;
- the host asleep, powered off or offline, since nothing on it can send anything;
- a request Sonarr or Radarr accepted and then waits on forever, because no release ever turns up.
  Seerr has no event for this, so it just sits there;
- the drive filling up. SABnzbd pauses downloading once free space reaches its `download_free`, and
  nothing tells anyone why: requests simply stop arriving.

`monitoring/stack_watch.py` covers all five. It sends to the same ntfy topic as everything else.
It is one standard-library Python script run by two systemd user timers, with no new container and no
new account.

## What it sends

| Notification | When | Priority |
| ------------ | ---- | -------- |
| **Media stack on *host*: N problems** | A problem showed on two checks, at least 10 minutes apart. A drive under `DISK_FREE_MIN` free is one of them | High if new, else Default |
| **… all clear** | Everything above has been fine for two checks | Low |
| ***host* is unreachable** | No check-in for `HEARTBEAT_GRACE`; ntfy.sh sends it | High |
| ***host* is back online** | The first check after "unreachable" went out | Default |
| **N wanted titles not downloaded after 3 days** | Daily, when a title joins the list or is owed a reminder | Default |
| **Stack watch on *host* is failing** | The script failed two runs in a row; repeated daily | High |
| **Stack watch on *host* started over** | Its state file was unreadable, so it was moved aside | Default |

- **Problems** are a Compose service that is missing, exited, restarting or unhealthy; Jellyfin's
  `/health` not saying `Healthy`; less than `DISK_FREE_MIN` free where the library or the downloads
  live; Docker not answering; or a Compose file that can't be read. While Docker isn't answering,
  services keep the state they last had rather than being counted as recovered.
- **The disk problem** reads the filesystems behind `MEDIA_ROOT` and `SABNZBD_TEMP`. Paths that share
  a filesystem share one entry (`disk: dropped below 50G free for the library and downloads`); paths
  on separate drives get one each. It reports the *lowest* step the drive has been under since the
  problem began — `DISK_FREE_MIN`, then half, a quarter and a tenth of it — never the figure itself,
  and never moving back up until the all clear releases it. That is why it says "dropped below": free
  space rising past a step again doesn't make the sentence untrue, so there is nothing to republish.
  Without the ratchet there would be. A description is taken from the latest check with no damping,
  and a changed one republishes the notification, so a drive drifting across a step alternates:
  measured, a ±2G wobble across the 50G step sent 10 updates in 11.5 hours and left almost none of
  the daily cap for the fall that followed. SABnzbd pausing at its own `download_free` and resuming
  when space returns is a mechanism for producing exactly that wobble, around exactly that number.
  Ratcheted, the same wobble sends one, and the fall afterwards still gets each remaining step.
  Neither path is named: the topic is public and the paths name your home directory.
- **The problem notification is updated in place**, and so are the heartbeat and "failing" ones (ntfy's
  `sequence_id`), so a problem that lasts a day is one notification that changes. A new problem goes out
  at once. Anything else (a problem clearing, or its description changing) waits until an hour after
  the previous update, so a flapping check can't send an alert and an all clear every 30 minutes. It
  is updated at most 12 times a day, and the 12th update says until when the rest are muted. A problem
  that hasn't been reported in the last day still goes out while muted, so a flapping check can't hide
  a container that dies for real.
- **The wanted-titles digest** lists monitored movies and episodes with no file and nothing in the
  download queue, `STALL_DAYS` after they were added or released, whichever is later. New titles come
  first. Each title gets a reminder every `STALL_REMIND_DAYS`, at most twice.
- **"Failing"** says what failed in words the script wrote, such as `radarr: HTTP 401`, never an error
  message. "Working again" replaces it when the script recovers.

Nothing from an error message or a command's output goes into a notification: anyone who knows the
topic can read it, and Docker Compose quotes `.env` values (credentials) in its errors. Those details
are in the journal.

## How the host-offline alert works

A host can't report that it's gone, so it has to be reported from somewhere else. ntfy's scheduled
messages do that without a second service. The check keeps an "unreachable" message scheduled on ntfy,
and each new one [replaces the one still
waiting](https://docs.ntfy.sh/publish/#updating-scheduled-notifications) because they share a sequence ID.
While the host keeps checking in, the delivery time keeps moving forward and nothing arrives. When the
checks stop, the last scheduled message is delivered.

ntfy.sh allows an anonymous IP **250 messages a day**, shared with every service on the host that
publishes, so the message isn't rescheduled on every check. A check moves it only once the time left
before delivery drops to `HEARTBEAT_GRACE`, and then moves it to `HEARTBEAT_GRACE` plus an hour from now.
That means:

- the heartbeat costs **24 messages a day**, whatever the grace;
- "unreachable" goes out between `HEARTBEAT_GRACE` and `HEARTBEAT_GRACE` + 1 hour after the last check-in,
  never sooner.

With the problem notification capped at 12 updates a day, the stack watch's worst case is about 40 of the
250.

A changed `HEARTBEAT_GRACE` takes effect on the next check, with no reinstall. The script stores when the
scheduled message will go out, so a shorter or longer grace just reschedules it and never sends a false
"back online".

`HEARTBEAT_GRACE` can be `30m` to `71h` (ntfy.sh schedules at most 3 days ahead, less the hour), or `off`.
`off` cancels the scheduled message on the next check, within 15 minutes, and so does uninstalling.
Stopping the timers by hand does **not** cancel it; to do that, run
`python3 monitoring/stack_watch.py disarm`.

**Pick the grace for how the host is actually used.** On an always-on server, `1h` reports a crash one
to two hours after it happens. A laptop that sleeps will alert every time a sleep outlasts the grace, so
pick something longer than its normal sleeps, or turn it `off` and rely on the container and Jellyfin
checks.

## Setup

1. Set up the ntfy topic first ([Notifications](Notifications), step 1), and subscribe to it on your phone.
2. Add the topic to `.env`. The rest is optional; `.env.example` documents each setting.

   | Variable | Default | Meaning |
   | -------- | ------- | ------- |
   | `NTFY_TOPIC` | required | Topic to publish to |
   | `HEARTBEAT_GRACE` | `24h` | Silence before "unreachable": `30m` to `71h`, or `off` |
   | `DISK_FREE_MIN` | `100G` | Free space below which the drive is a problem: `1G` or more, as `G`/`T`, or `off` |
   | `STALL_DAYS` | `3` | Days a wanted title may wait before the digest lists it |
   | `STALL_REMIND_DAYS` | `7` | Days between reminders about the same titles |
   | `JELLYFIN_URL` | `http://localhost:8096` | Jellyfin from this host, or `off` |
   | `WATCH_HOSTNAME` | the hostname | Name in titles and the heartbeat's sequence ID; see below |
   | `NTFY_SERVER` | `https://ntfy.sh` | A self-hosted ntfy server; use https |
   | `SONARR_URL`, `RADARR_URL` | from `config.xml` | For an *arr that isn't on `localhost` at its configured port and URL base |
   | `STATE_DIR` | `~/.local/state/media-stack-watch` | Where the script keeps its state |

   **`DISK_FREE_MIN` is an amount, not a percentage**, because what it has to protect is an amount.
   SABnzbd stops downloading when its **temp** folder's filesystem reaches its own `download_free`
   (50G in this stack) and says nothing anyone sees, so the alert has to come far enough above that to
   leave room to act. On this host `SABNZBD_TEMP` and `MEDIA_ROOT` are the same drive, so one number
   covers both; if you put temp on its own disk, that is the one `download_free` governs. At this stack's 1080p WEB
   profile a film measures 3–9G and a season 10–27G, so the default `100G` is SABnzbd's floor plus
   the largest season plus slack — and it still means that on a larger drive, where the same
   percentage would be tens of titles of false headroom. `G` is 2^30 bytes, as in `df -h`.

   Blank values use the default; only `off` turns a check off. API keys are read from each service's
   `config.xml` under `CONFIG_ROOT` (a relative `CONFIG_ROOT` is taken from the repo root, as Compose
   does). A missing `config.xml` is reported as a failure rather than skipped.

   **Before changing `WATCH_HOSTNAME`, or the machine's hostname, run
   `python3 monitoring/stack_watch.py disarm`.** The heartbeat's sequence ID is built from the name, so the
   alert scheduled under the old name would otherwise still go out. The name is in every notification.

3. Install, as the user that runs Docker (no sudo):

   ```bash
   ./monitoring/install-stack-watch.sh
   ```

   It runs the digest and then a check once by hand. If either fails on a first install, it cancels the
   heartbeat that check may have scheduled and enables nothing. On an update, the timers from the previous
   install stay enabled and run the new script. Then it enables `media-stack-watch.timer` (every 15
   minutes) and `media-stack-stalled.timer` (daily at 10:00). Both have `Persistent=true`, so a run that
   was due while the machine slept happens when it wakes.

   User timers only run while your user's systemd instance does. If the installer warns that linger is
   off, run `sudo loginctl enable-linger $USER`, or the watch stops whenever you log out.

To remove it: `./monitoring/install-stack-watch.sh --uninstall`.

Logs are in `journalctl --user -u 'media-stack-*'`, and state is in `~/.local/state/media-stack-watch/`.

## Testing

Force each alert once after installing. `--test` prefixes titles with `TEST:`, uses its own state file and
sequence IDs, and drops the spacing between alerts (the 10-minute span, the hour between updates, the hour
added to the heartbeat), so back-to-back runs force an alert and a grace as short as `10s` is allowed.
`--dry-run` prints what would be sent and saves nothing.

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

   Then start it again. The next two checks send "… all clear".

   ```bash
   docker start bazarr
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   ```

   Stopping the container is real: the installed timer sees it too. **Start it again within 15 minutes**,
   or the real watch sends a real alert.

2. **Jellyfin not answering**, by pointing at a closed port twice, then two normal checks for the all clear.

   ```bash
   JELLYFIN_URL=http://localhost:1 HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   JELLYFIN_URL=http://localhost:1 HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   ```

3. **Docker not answering**, the same way.

   ```bash
   DOCKER_HOST=unix:///nonexistent.sock HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   DOCKER_HOST=unix:///nonexistent.sock HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   ```

4. **Host unreachable.** Schedule a 30-second heartbeat and don't check in again:

   ```bash
   HEARTBEAT_GRACE=30s python3 monitoring/stack_watch.py check --test
   ```

   Wait a minute; "TEST: *host* is unreachable" arrives. Then check in, which sends "back online" and
   schedules another test alert. Cancel that one **5 to 25 seconds later**: ntfy.sh ignores a cancel that
   arrives in the same second as the message it cancels, and the test alert goes out after 30.

   ```bash
   HEARTBEAT_GRACE=30s python3 monitoring/stack_watch.py check --test
   ```

   ```bash
   python3 monitoring/stack_watch.py disarm --test
   ```

5. **The drive running out of room**, with a threshold no drive can satisfy, then two normal checks
   for the all clear:

   ```bash
   DISK_FREE_MIN=1024T HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   DISK_FREE_MIN=1024T HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   ```

6. **Stalled digest**, treating everything wanted as overdue:

   ```bash
   STALL_DAYS=0 python3 monitoring/stack_watch.py stalled --test
   ```

7. **The watch failing**, with a setting it rejects, twice, then a clean run for "working again":

   ```bash
   HEARTBEAT_GRACE=never python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=never python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   ```

Two checks that a green test suite can't give you, both run from the repo root and both harmless
(neither touches the repo, and neither sends anything):

```bash
python3 monitoring/mutation_table.py   # breaks one behaviour at a time; a SURVIVOR is untested
python3 monitoring/simulate_disk.py    # how many ntfy messages a filling or wobbling drive costs
```

`mutation_table.py` is worth running after changing `stack_watch.py`: a passing suite shows the tests
ran, not that they'd notice a regression. It has caught real gaps here — a blank `DISK_FREE_MIN`
(what `.env.example` ships) silently turning the disk check off, among others. Add an entry when you
add behaviour worth keeping.

To see what the topic received, poll the cache with
`curl -s "https://ntfy.sh/<topic>/json?poll=1&since=10m"`.
Add `&scheduled=1` to also list messages still waiting to be sent.

## Not covered

- **A download stuck in the queue.** Anything in the queue counts as progress, including a download that
  has stalled at 0 B/s. SABnzbd's failure alert and the *arrs' "manual interaction required" alert cover a
  download that fails or can't be imported, but not one that never finishes.
- **ntfy.sh itself.** Every alert here, and the offline alert especially, depends on ntfy.sh being up.
  Anyone who knows the topic can also cancel the scheduled heartbeat, since its sequence ID is predictable.
- **A disk that fills between two checks.** `DISK_FREE_MIN` is a warning with room to act in it, not a
  guarantee: a large grab can cross it and SABnzbd's own floor inside one 15-minute interval. Once the
  drive is genuinely full the script can't save its state either, so it sends nothing rather than
  repeating itself every run, and the scheduled "unreachable" goes out once the grace has passed.
- **A filesystem the stack writes to that is neither `MEDIA_ROOT` nor `SABNZBD_TEMP`**, such as a
  `CONFIG_ROOT` on its own drive.
- **A drive that failed to mount.** An unmounted mount point is an ordinary empty directory, so the
  disk check reports the filesystem underneath it and sees plenty of room, and nothing here says
  otherwise. Worth knowing when `MEDIA_ROOT` moves onto its own drive.
- **A container stuck in `health: starting`** counts as fine. Docker normally turns that into
  `unhealthy` once its retries run out.
