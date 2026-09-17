# Stack Watch

The services' own [notifications](notifications.md) cover what each one can see about itself: a failed
download, an indexer health error, a request Seerr couldn't hand off. Some failures can't report
themselves:

- a container that has stopped or turned unhealthy, since it can't send its own alert;
- Jellyfin not answering, since it runs outside the stack;
- the host asleep, powered off or offline, since nothing on it can send anything;
- a request Sonarr or Radarr accepted and then waits on forever, because no release ever turns up.
  Seerr has no event for this, so it just sits there;
- the drive filling up. SABnzbd pauses downloading once free space reaches its `download_free`, and
  nothing tells anyone why: requests simply stop arriving;
- Seerr running without the patch its image is built with
  ([images/seerr](../images/seerr/README.md)). Its healthcheck passes either way, and without the patch
  a requester's progress bar freezes during the very download they are waiting on.

`monitoring/stack_watch.py` covers all six. It sends to the same ntfy topic as everything else.
It is one standard-library Python script run by two systemd user timers, with no new container and no
new account.

## What it sends

| Notification | When | Priority |
| ------------ | ---- | -------- |
| **Media stack on *host*: N problems** | A problem showed on two checks, at least 10 minutes apart. A drive under `DISK_FREE_MIN` free is one of them, and says what it has free now. Names anything that recovered in the same breath | High if new or worse, else Default |
| *(the same notification, re-sent)* | The same problems are still unresolved a day later. Not a separate alert — same title, replaced in place | Default |
| **… all clear** | Everything above has been fine for two checks. Names what recovered | Low |
| ***host* is unreachable** | No check-in for `HEARTBEAT_GRACE`; ntfy.sh sends it | High |
| ***host* is back online** | The first check after "unreachable" went out | Default |
| **N wanted titles not downloaded after 3 days** | Daily, when a title joins the list or is owed a reminder | Default |
| **Stack watch on *host* is failing** | The script failed two runs in a row; repeated daily | High |
| **Stack watch on *host* started over** | Its state file was unreadable, so it was moved aside | Default |

- **Problems** are a Compose service that is missing, exited, restarting or unhealthy; Jellyfin's
  `/health` not saying `Healthy`; less than `DISK_FREE_MIN` free where the library or the downloads
  live; Docker not answering; or a Compose file that can't be read. While Docker isn't answering,
  services keep the state they last had rather than being counted as recovered. Only containers
  Compose created count: a `docker run` of an image Compose built carries the project's labels, but
  not the ones Compose puts on its own containers, so it can't stand in for a stopped service.
- **The Seerr patch problem** reads `/app/dist/lib/downloadtracker.js` out of the running `seerr`
  container (`docker exec` by the Id of the container the service check found running, 5s timeout)
  and says `seerr: running without its download-tracker patch` if `refreshMonitoredDownloads` is in
  it. A read that fails, or a file with no `getQueue` call in it, is its own problem, since the patch
  is an absence and an empty file has that too. Once an unpatched alert has gone out, such a read keeps
  the "running without" wording rather than changing it, so a timed-out read doesn't cost two High
  updates. Before that first alert it says what it is: "seen unpatched once, then couldn't re-check"
  is a different and more urgent fault than an unpatched Seerr — Docker may not be able to get into
  the container at all — and the wording is pinned to protect a message already sent, not to settle a
  question still open. It still counts as a sighting, so only a read that finds the patch clears it —
  and because a sighting resets the absence damping, a Seerr that really was rebuilt and then went
  unreadable keeps reporting as unpatched for as long as the reads keep failing. That is the trade:
  the alternative is reading a failed exec as recovery. It is only asked
  when the Compose project has a `seerr` service that isn't already reported as stopped or unhealthy —
  one problem, not two, for a stopped container — and while that, or Docker being down, stops it being
  asked, it keeps its last state instead of being counted as fixed.
- **The disk problem** reads the filesystems behind `MEDIA_ROOT` and `SABNZBD_TEMP`. Paths that share
  a filesystem are worded as one (`disk: dropped below 50G free for the library and downloads`); paths
  on separate drives get one line each. Leaving `SABNZBD_TEMP` blank does **not** mean the downloads
  filesystem goes unwatched: `docker-compose.yml` mounts `${SABNZBD_TEMP:-/tmp/sabnzbd-temp}`, so the
  check follows it to the same place — unless that filesystem is smaller than `DISK_FREE_MIN`, which
  the usual `/tmp` tmpfs is. It could never have that much free, so watching it would be a standing
  alert nobody can clear, on the same notification as the real library-full one; it is skipped with a
  line in the journal instead. A path you configured yourself is always watched as asked, whatever
  its size. If the fallback directory doesn't exist, nothing is watched and nothing is said — but
  only while it isn't already a problem. Once it is, the path going quiet is *not* recovery, and it
  is reported as unreadable rather than dropped; dropping it would let the problem decay into an all
  clear naming it as "Cleared", on the strength of nothing but the path having stopped answering.
  (One case is deliberately left the other way: a fallback that has become *too small* for the
  threshold while it was already a problem is dropped, and does send that false "Cleared". Holding it
  would be the standing unclearable alert above. It takes raising `DISK_FREE_MIN` past the whole size
  of the fallback filesystem, or moving the fallback onto a small one, and the journal says so.)
  It reports the *lowest* step the drive has been under since the
  problem began — `DISK_FREE_MIN`, then half, a quarter and a tenth of it — never the figure itself,
  and never moving back up until the all clear releases it. That is why it says "dropped below": free
  space rising past a step again doesn't make the sentence untrue, so there is nothing to republish.
  Without the ratchet there would be. A description is taken from the latest check with no damping,
  and a changed one republishes the notification, so a drive drifting across a step alternates:
  measured, a ±2G wobble across the 50G step sent 10 updates in 11.5 hours and left almost none of
  the daily cap for the fall that followed. SABnzbd pausing at its own `download_free` and resuming
  when space returns is a mechanism for producing exactly that wobble, around exactly that number.
  Ratcheted, the same wobble sends one, and the fall afterwards still gets each remaining step.
  What the ratchet costs is that the sentence is about the low point, not about now, and after a day
  it reads as though it were about now. So every disk line also quotes what the drive actually has —
  `disk: dropped below 10G free for the library (now 70G free)`. The figure is carried *beside* the
  sentence and never inside it: in it, it would change on nearly every check and bring the wobble
  back. It is read fresh each run, so a drive that stopped answering has no figure rather than
  yesterday's, and it is rounded to four significant digits, because a filesystem answers with all of
  them (`142.5914421G` is what one live check produced before that was fixed).
  Neither path is named: the topic is public and the paths name your home directory. A configured
  path that can't be read is reported as `disk: can't read free space for the … path`, and a drive
  that has spun down or gone stale is given up on after 20 seconds rather than blocking the run —
  the disk is checked last, so a read that never returned used to cost the container and Jellyfin
  results gathered before it.
- **The problem notification is updated in place**, and so are the heartbeat and "failing" ones (ntfy's
  `sequence_id`), so a problem that lasts a day is one notification that changes. A new problem goes out
  at once. Anything else (a problem clearing, or its description changing) waits until an hour after
  the previous update, so a flapping check can't send an alert and an all clear every 30 minutes. It
  is updated at most 12 times a day, and the 12th update says until when the rest are muted. A problem
  that hasn't been reported in the last day still goes out while muted, so a flapping check can't hide
  a container that dies for real.
- **A problem that is still there a day later is sent again**, saying "Still not resolved" and when it
  was first seen. Otherwise a problem is announced exactly once, ever: the notification only goes out
  when something about it changes, and a description that has settled never changes again — which the
  disk's ratchet makes the normal case rather than the exception. A drive parked at 5G free would
  be a single notification, possibly swiped away weeks ago, with no all clear coming until somebody
  frees space. Measured with `simulate_disk.py`, a drive left alone for a week costs 7 messages
  rather than 1, against ntfy.sh's 250 a day. That one-a-day ceiling is structural rather than a
  matter of the 12-a-day cap, which can never mute a re-send: a re-send only happens when nothing
  has gone out for a day, and by then every entry in the day's tally has aged out of it.
  It says "still not resolved" and not "unchanged" on purpose — the report ratchets to the low point
  of the episode, so someone who has just freed 60G and is still under `DISK_FREE_MIN` sees the same
  sentence as before, and should not also be told that nothing has changed.
- **A recovery is named whether or not it is the last one.** A notification that still has problems
  ends with a `Cleared:` block listing what stopped, and the all clear does the same. It used to be
  only the all clear, so a problem recovering while another remained was erased — the bullet stopped
  appearing, in a notification whose title went from "3 problems" to "2 problems" and said nothing
  about which. Because the disk report ratchets, freeing 60G to go from 10G to 70G free changes
  nothing until `DISK_FREE_MIN` itself is cleared, so for the disk this is the only acknowledgement a
  recovery gets, and waiting for the *last* problem to go made it the system's schedule rather than
  yours.
- **A role moving to a different drive goes out at High**, even when the new drive is healthier. The
  floor deliberately belongs to a filesystem and not to a role, so moving `MEDIA_ROOT` from a drive
  at 5G to one at 60G re-reports from the top step, and any changed description counts as worsening.
  Ranking the two would mean comparing a level measured on one drive against a level measured on
  another, which is the falsehood the per-filesystem floor exists to prevent. The drive really is
  under the threshold; the line says what it actually has.
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
  never sooner — unless that check-in's own reschedule failed, in which case the message already waiting
  goes out when it was due, which can be hours early after a long sleep (see [Not covered](#not-covered)).

With the problem notification capped at 12 updates a day, the stack watch's worst case is about 40 of the
250.

A changed `HEARTBEAT_GRACE` takes effect on the next check, with no reinstall. The script stores when the
scheduled message will go out, so a shorter or longer grace just reschedules it and never sends a false
"back online".

`HEARTBEAT_GRACE` can be `30m` to `70h`, or `off`. ntfy.sh schedules at most 3 days ahead; the ceiling is
that limit less the hour the alert is scheduled past grace, and less another hour of margin. The margin is
not fussiness: a delay *at* the limit is rejected outright rather than trimmed, so the two clocks
disagreeing by a second stops the heartbeat rescheduling at all — the dead man's switch failing shut, at
the setting chosen to make it wait longest.

`off` cancels the scheduled message on the next check, within 15 minutes, and so does uninstalling.
Stopping the timers by hand does **not** cancel it; to do that, run
`python3 monitoring/stack_watch.py disarm`.

`disarm` confirms the cancellation rather than assuming it. ntfy.sh answers `200` to a cancel it ignored
just as it does to one it honoured, and it ignores one arriving in the same second as the message it
cancels — so a cancel right after a check, which is exactly what the installer and `--uninstall` do, used
to leave the alert armed and silently report success. It now waits out that second, cancels, asks the
server what is still scheduled, and tries again if the answer is "yours". It exits non-zero when the alert
is *known* to be still armed, when the cancel could not be sent, or when the cancel worked but the state
file couldn't be updated to say so afterward (an unreadable or full disk); a cancel that went out but could
not be confirmed by the server is reported on stderr and treated as success. Expect it to take about five
seconds when it follows a check closely, and to return immediately otherwise.

**Pick the grace for how the host is actually used.** On an always-on server, `1h` reports a crash one
to two hours after it happens. A laptop that sleeps will alert every time a sleep outlasts the grace, so
pick something longer than its normal sleeps, or turn it `off` and rely on the container and Jellyfin
checks.

## Setup

1. Set up the ntfy topic first ([Notifications](notifications.md), step 1), and subscribe to it on your phone.
2. Add the topic to `.env`. The rest is optional; `.env.example` documents each setting.

   | Variable | Default | Meaning |
   | -------- | ------- | ------- |
   | `NTFY_TOPIC` | required | Topic to publish to |
   | `HEARTBEAT_GRACE` | `24h` | Silence before "unreachable": `30m` to `70h`, or `off` |
   | `DISK_FREE_MIN` | `100G` | Free space below which the drive is a problem: `1G` or more, as `G`/`T`, or `off` |
   | `STALL_DAYS` | `3` | Days a wanted title may wait before the digest lists it |
   | `STALL_REMIND_DAYS` | `7` | Days between reminders about the same titles |
   | `JELLYFIN_URL` | `http://localhost:8096` | Jellyfin from this host, or `off` |
   | `WATCH_HOSTNAME` | the hostname | Name in titles and the heartbeat's sequence ID; see below |
   | `NTFY_SERVER` | `https://ntfy.sh` | A self-hosted ntfy server; use https |
   | `SONARR_URL`, `RADARR_URL` | from `config.xml` | For an *arr that isn't on `localhost` at its configured port and URL base, or `off` if this stack doesn't run it |
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

**How long a run can take.** Both units have `TimeoutStartSec=10min`. systemd kills a run that
outlasts that, and nothing the run decided is saved. Every network request and command a run makes
has a hard limit:
- Docker commands: 60 seconds, except the read of Seerr's tracker (5).
- Jellyfin: 10 seconds.
- Each free-space read: 20 seconds.
- Each ntfy or *arr request: 20 seconds, whether it is still resolving the name or the reply is
  still trickling in.

`RunTimeBudgetTest` adds these limits up for every state a run can start from and every call it can
fail at. The longest possible run comes to:
- a check at 315 seconds;
- a stalled run at 120 seconds, plus 20 for every page of an *arr's queue or wanted list beyond the
  first (250 titles a page).

The test fails when a new check or a raised timeout leaves less than 30 seconds of margin. The limit
also has to stay under the 15-minute check interval, because systemd skips a tick while the previous
run is still going. An install made before the limit was raised from 5 minutes keeps the old limit
until the installer is run again.

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
   schedules another test alert. Cancel that one straight away — `disarm` waits out the second in which
   ntfy.sh ignores a cancel, and then confirms with the server that the alert really is gone, so it
   takes about five seconds and needs no counting. If it cannot cancel it, it says so and exits
   non-zero; silence means the alert is gone, not that a request was sent.

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

6. **A recovery while another problem remains**, and the figure each drive has now. Point the two
   roles at two different filesystems, put both under an impossible threshold, then lower it so only
   one is still under. `/tmp` is a separate filesystem on most hosts; check with `df` first.

   ```bash
   export SABNZBD_TEMP=/tmp HEARTBEAT_GRACE=off
   DISK_FREE_MIN=1024T python3 monitoring/stack_watch.py check --test   # x2: "2 problems"
   DISK_FREE_MIN=100G python3 monitoring/stack_watch.py check --test    # x2: "1 problem" + "Cleared:"
   ```

   Each disk line should end in `(now …G free)` with a rounded figure, and the line for a drive that
   was *not* read on that run should have no figure at all rather than the previous one.

7. **A vanished fallback**, which must not read as a recovery. This needs a threshold the fallback
   filesystem is under but is not *smaller* than (or it is skipped as unwatchable), so read `df -B1`
   on it and pick a size between its free and its total.

   ```bash
   export SABNZBD_TEMP= HEARTBEAT_GRACE=off DISK_FREE_MIN=15.4G   # sized from df, see above
   python3 monitoring/stack_watch.py check --test                 # x2: it alerts
   mv /tmp/sabnzbd-temp /tmp/sabnzbd-temp.aside
   python3 monitoring/stack_watch.py check --test                 # x3
   mv /tmp/sabnzbd-temp.aside /tmp/sabnzbd-temp
   ```

   The right answer is `disk: can't read free space for the downloads path` and **no all clear**.
   Only do this while `SABNZBD_TEMP` is really set in `.env` to something else, so the path you are
   moving is not one the running stack has mounted.

8. **Stalled digest**, treating everything wanted as overdue:

   ```bash
   STALL_DAYS=0 python3 monitoring/stack_watch.py stalled --test
   ```

9. **The watch failing**, with a setting it rejects, twice, then a clean run for "working again":

   ```bash
   HEARTBEAT_GRACE=never python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=never python3 monitoring/stack_watch.py check --test
   HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test
   ```

10. **Seerr without its patch.** Never unpatch the real `seerr` to test this. Put the call back into
    a throwaway container from the same image, and point only the check's `docker exec` at it. The
    image is named after the Compose project; `docker inspect seerr --format '{{.Config.Image}}'`
    prints it.

    ```bash
    docker run -d --name seerr-negctl --network none --entrypoint sleep docker-media-server-seerr 600
    docker exec -u root seerr-negctl sed -i \
      's/^\( *\)const queueItems = await radarr.getQueue();/\1await radarr.refreshMonitoredDownloads();\n&/' \
      /app/dist/lib/downloadtracker.js
    cat > /tmp/negctl.py <<'EOF'
    import os, sys
    sys.path.insert(0, "monitoring")
    import stack_watch as sw
    def run(argv, **kw):
        if argv[:2] == ["docker", "exec"]:  # the only exec is the tracker read, by container Id
            argv = ["docker", "exec", "seerr-negctl", *argv[3:]]
        return sw.run_cmd(argv, **kw)
    sys.exit(sw.main(["check", "--test"], environ=dict(os.environ, HEARTBEAT_GRACE="off"), run=run))
    EOF
    python3 /tmp/negctl.py   # x2: "TEST: … 1 problem", "seerr: running without its download-tracker patch"
    HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test   # x2: "… all clear"
    docker rm -f seerr-negctl
    ```

    **To force the other branch — one unpatched sighting, then a read that fails — remove the
    container between the two runs instead of after them:**

    ```bash
    python3 /tmp/negctl.py       # 1st: the sighting. Nothing is sent yet.
    docker rm -f seerr-negctl    # now the exec has nothing to talk to
    python3 /tmp/negctl.py       # 2nd: "seerr: can't check its download-tracker patch"
    HEARTBEAT_GRACE=off python3 monitoring/stack_watch.py check --test   # x2: "… all clear"
    ```

    The second run is the one that publishes, and it must say `can't check`, not `running without`:
    the wording is held only for an alert that has already gone out. Getting any `seerr:` patch
    sentence at all also proves the redirect was in place — without it, the removed container would
    be reported as `seerr: no container` and the patch check would never be asked.

The daily re-send is deliberately not forceable this way: `--test` drops the spacing *between*
different alerts, and giving it a zero re-send interval too would make every repeated `--test` check
in the steps above publish a duplicate. If you do need to see one, run step 5's first two commands,
then set `stack.sent` in `$STATE_DIR/check-test.json` to a timestamp over a day old and run the
first command once more.

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
  guarantee: a large enough grab can cross more than one step between checks. For scale, the fastest
  fill measured on this host was ~21 GiB/h while a season was downloading, or ~5 GiB per 15-minute
  check — enough to skip a step, not enough to clear `DISK_FREE_MIN` and SABnzbd's floor together.
  Worth re-checking on a much faster connection. Once the drive is genuinely full the script can't
  save its state either, so it sends nothing rather than repeating itself every run, and the
  scheduled "unreachable" goes out once the grace has passed.
- **A filesystem the stack writes to that is neither `MEDIA_ROOT` nor `SABNZBD_TEMP`**, such as a
  `CONFIG_ROOT` on its own drive.
- **A drive that failed to mount.** An unmounted mount point is an ordinary empty directory, so the
  disk check reports the filesystem underneath it and sees plenty of room, and nothing here says
  otherwise. Worth knowing when `MEDIA_ROOT` moves onto its own drive.
- **Seerr on a stale base.** `docker compose up -d --pull always` or `--no-build` keeps running the
  last built image, which still has the patch, so the Seerr check passes. It proves the patch is
  there, not that the image is current. See [images/seerr](../images/seerr/README.md).
- **A Docker command stuck in uninterruptible sleep.** When a command times out it is killed and
  then waited for, but a process in D-state does not die. Waiting for it can outlast the unit's time
  limit, and then systemd kills the run with nothing it decided saved. The next check starts again
  from the state before. Free-space reads, the other calls that can block this way, are abandoned
  instead.
- **Reading files on a hung mount.** The state file, `.env` and the *arrs' `config.xml` are read
  with no limit. On a host where `CONFIG_ROOT` shares the media drive, a mount that hangs can hold
  the run until systemd kills it.
- **A very long wanted list or queue.** The digest pages through each one in full. On top of its
  120-second worst case, the 10-minute limit leaves room for about 22 extra pages (roughly 5,500
  titles). A backlog larger than that, on an *arr slow to answer, can outlast the limit.
- **A container stuck in `health: starting`** counts as fine. Docker normally turns that into
  `unhealthy` once its retries run out.
- **An inferred fallback that becomes too small for the threshold while it is already a problem.**
  It stops being watched, and that reads as a recovery it never had. The alternative is the standing
  unclearable alert the size check exists to remove, so this one is deliberate rather than open; see
  the disk bullet above. It takes raising `DISK_FREE_MIN` past the fallback filesystem's whole size,
  or moving the fallback onto a small one.
- **A disk that fills partway through a check.** The run proves the state file can be written before it
  sends anything, so the common case — a disk already full — sends nothing. A disk that fills between
  that proof and the final save is not covered: the run's alerts have gone out but nothing it decided is
  recorded, so the next run decides the same things again, held down only by the 12-a-day cap. It is
  reported in the journal and the unit exits non-zero. Nothing is notified, on purpose: a notification
  is the one thing that would repeat every run, since suppressing it needs the state that cannot be
  written.
- **A reschedule that fails while the host is up.** If the POST that moves the scheduled alert fails,
  the run is recorded as a failure and the next check retries 15 minutes later. The alert is not at risk
  in between: a failed publish changes nothing on the server, and the message already waiting there is
  never further away than grace + 1 hour, so if the host stops now it still fires — never late, but
  possibly *early*. Normally that is by less than one check. On the catch-up check after a long sleep,
  though, the waiting message may be only hours away (a 20h sleep under a 24h grace leaves about 5h),
  so a failed reschedule there followed by the host sleeping again can send "unreachable" hours
  before the grace has passed. The alert is true, since the host is away, but it comes sooner than the
  grace promises. A host that stays up but cannot reach ntfy.sh for a whole grace period gets
  that "unreachable" alert, and it is **true**: from the phone's point of view, a host that cannot reach
  the notification service is exactly as unreachable as one that is asleep.
- **A failed re-arm straight after "back online" — accepted, not open (decided 2026-09-16).** When a
  check finds the last "unreachable" already delivered, nothing is waiting on ntfy any more. It sends
  "back online", then schedules the next "unreachable". If only that second publish fails, **nothing is
  scheduled at all** (`test_back_online_is_not_repeated_when_rescheduling_fails`) until a later check
  publishes successfully, normally 15 minutes on. If the host goes away inside that window and stays
  away past the grace, that absence is **not reported at all** — the phone's last word is "back
  online" — and a single failed run is below the two it takes to send "is failing". Unlike the case
  above, a host that stays up but keeps failing to reach ntfy.sh is not reported either, because
  nothing is waiting on the server to go out. It is accepted because:
  - **No change on this host closes it.** Publishing the reschedule first doesn't work: "back online"
    goes out on the same sequence and would replace it. State can't help: the server is unarmed
    whatever the state file says. And a host can always die just after its last publish fails.
  - **A retry would only narrow it, for what it costs.** The failing publish comes milliseconds after
    one that succeeded, so the network was up; what fails there is mostly a blip that the next check
    clears anyway, or a lid closed right after the catch-up check at wake (`Persistent=true` runs one),
    which no retry survives.
  - **It is rare and bounded.** It needs an absence longer than the grace (a few a month here at the
    24h default), then a failure on exactly the second of two back-to-back publishes, then another
    absence longer than the grace starting within 15 minutes. It misses at most that one outage; the
    first successful check after it re-arms.
  - **The real fix is heavy.** Alternating two sequence IDs would let a check arm the next alert
    *before* announcing "back online" and leave "unreachable" showing if that fails. But state,
    `disarm` and `pending()` would all have to handle both IDs. Revisit this if a grace shorter
    than the host's normal sleeps is ever used, since "back online" then happens most days.
- **A blip immediately before a short sleep.** Two sightings 15 minutes apart alert, and a sleep of
  20-30 minutes is indistinguishable from one missed check: a problem seen once just before the host
  slept and once on the catch-up run at wake can alert and then clear, for something that was only ever
  Docker thawing. Telling the two apart needs the host to record that it slept, which it does not.
