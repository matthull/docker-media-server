#!/usr/bin/env python3
"""Stack watch: the alerts this stack's services cannot send about themselves.

  check    Every CHECK_INTERVAL. Alerts when a Compose service is stopped or unhealthy, Seerr
           is running without its download-tracker patch, Jellyfin stops answering, the drive
           the library and downloads live on is running out of room, or Docker itself is down. Also keeps an ntfy dead man's switch scheduled that fires if
           this host stops checking in (asleep, powered off, offline).
  stalled  Daily. Alerts on monitored items Sonarr/Radarr still have no file for after
           STALL_DAYS. Nothing else reports this: a request can be accepted and then wait
           forever for a release that never comes.
  disarm   Cancels the dead man's switch. Needed whenever the timers stop for good, or the
           last scheduled "unreachable" alert still goes out.

Standard library only. Settings come from the stack's .env, with the process environment
taking precedence; API keys are read from each service's config.xml. See docs/stack-watch.md.

Nothing from an exception message or a command's stderr goes into a notification: anyone who
knows the topic can read it, and Compose quotes .env values (credentials) in its errors. Those
details go to the journal; notifications carry text this script wrote.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
# Imported as a name, not as http.client: `http` is a parameter name throughout this file (the
# injected request function), so the module would be shadowed exactly where it is needed.
from http.client import HTTPException
from pathlib import Path
from typing import Callable

STACK_DIR = Path(__file__).resolve().parent.parent

# How often `check` runs. install-stack-watch.sh schedules it (OnCalendar=*:0/15): change both.
CHECK_INTERVAL = timedelta(minutes=15)
# A problem, or a failing run of this script, must show on this many consecutive runs before it
# alerts, and an alerted problem must be gone for this many runs before "all clear". Absorbs container
# recreation on image updates, Docker still starting after boot, and a check that flaps.
CONSECUTIVE_RUNS = 2
# A sighting older than this does not count towards CONSECUTIVE_RUNS. One missed run is tolerated; two
# mean the host slept or the timer stopped in between, and Docker is often still thawing on both sides.
STALE_AFTER = CHECK_INTERVAL * 5 / 2
# ntfy.sh allows an anonymous IP 250 messages a day, shared with every service on the host that alerts.
# The stack notification is capped at this many updates a day; the last one says the rest are muted.
STACK_DAILY_CAP = 12
FAILURE_REPEAT = timedelta(days=1)  # "Stack watch is failing" is re-sent this often while it lasts
# An unchanged set of problems is re-sent this often. Without it a problem is announced exactly once,
# ever: publish_stack returns early while current == shown, and a description that has settled never
# changes again — which the disk's ratchet makes the normal case rather than the exception. A drive
# parked at 5G free would then be a single notification, possibly swiped away weeks ago, with no all
# clear coming until somebody acts. A day matches FAILURE_REPEAT, and costs one message a day out of
# ntfy.sh's shared 250 against the heartbeat's 24 and the stack notification's 12. That ceiling is
# structural, NOT a matter of STACK_DAILY_CAP, which can never mute a re-send: publish_stack prunes
# its record of sends to this same window, so the record is empty exactly when a re-send is due and
# the cap has nothing to count. Publishing refills it, which is what holds the rate to one a day.
# This constant is therefore real and retunable only because that prune follows it — an earlier
# version pruned at a flat day, which made every value above a day behave identically.
STACK_REPEAT = timedelta(days=1)
MAX_DELAY = timedelta(days=3)  # ntfy.sh's message-delay-limit
# Scheduling right at MAX_DELAY leaves no room for the clock difference between this host and
# ntfy.sh, and the delay is rejected outright rather than shortened: the publish 400s, the heartbeat
# is never rescheduled, and the dead man's switch the setting was raised to lengthen is the thing
# that breaks. Held back by a whole hour rather than a token minute so that the largest grace
# parse_grace will take is a round number, which is what the error message has to quote.
DELAY_MARGIN = timedelta(hours=1)
# ntfy.sh ignores a cancel that reaches it in the same second as the publish it cancels — measured
# against the service, and the reason docs/stack-watch.md tells a tester to wait before cancelling.
# `disarm` sits in exactly that window on two real paths: the installer cancelling a heartbeat the
# check it just ran had scheduled, and `--uninstall` landing seconds after a timer tick. Unhandled,
# the switch stays armed and an "unreachable" alert goes out about a host that was shut down on
# purpose — the one false alarm this check can produce that nobody can act on.
CANCEL_SETTLE = timedelta(seconds=5)
CANCEL_ATTEMPTS = 3  # each confirmed against the server, so this is a ceiling and not a cost
STALL_REMINDERS = 2  # reminders about a stalled title after the digest that first listed it
MAX_EPISODES_LISTED = 4  # a series missing more episodes than this is shown as a count
MAX_MESSAGE_BYTES = 4096  # ntfy delivers anything longer as an attachment
MAX_DETAIL_CHARS = 160  # longest library-written error detail quoted in a notification
LOW, DEFAULT, HIGH = 2, 3, 4  # ntfy priorities
SIZE_UNITS = {"M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
# The settings whose filesystems the disk check watches, what an alert calls each one, and the
# fallback docker-compose.yml applies when the setting is blank. Both are checked separately because
# either can be moved to its own drive. Compose mounts ${SABNZBD_TEMP:-/tmp/sabnzbd-temp}, so a blank
# setting doesn't mean "no downloads filesystem", it means that one — often a tmpfs, and the one
# SABnzbd's own download_free governs; watching nothing there would leave it unseen. MEDIA_ROOT is a
# bare ${MEDIA_ROOT} in Compose, so blank really is unconfigured. The word, never the path, is what
# goes out: the topic is public and the paths name the user's home directory.
DISK_ROLES = (("MEDIA_ROOT", "library", None), ("SABNZBD_TEMP", "downloads", "/tmp/sabnzbd-temp"))
# os.statvfs and os.stat have no timeout of their own and block uninterruptibly on a spun-down,
# failing or stale mount. Every timeout here is spent inside the unit's TimeoutStartSec, and
# RunTimeBudgetTest adds them all up: raise one or add a call, and that test says whether it still fits.
DISK_READ_TIMEOUT = 20
# Default DISK_FREE_MIN. SABnzbd pauses downloading at download_free (50G as shipped here) and says
# nothing anyone sees, so requests stop arriving silently; the alert has to come well before that.
# Measured on real releases at this stack's 1080p WEB profile: a film is 3-9G (the first real import
# was 3.4G) and a season is 10-27G. 100G leaves SABnzbd's floor plus the largest season plus room to
# act, and unlike a percentage it still means that on a bigger drive after the desktop migration.
DISK_FREE_MIN_DEFAULT = "100G"
# What a disk alert reports, as fractions of DISK_FREE_MIN: the lowest one the drive has been under
# since the problem began, never the figure itself. advance() damps whether a problem is there, but
# a description is taken from the latest run undamped, and a changed description republishes the
# stack notification. Free space is the first thing here that varies continuously, and it wobbles:
# SABnzbd pausing at its own download_free and resuming when space returns is a mechanism for
# wobbling it, and with the 100G default the 0.5 step is 50G, the very number it wobbles around.
# Measured, a +-2G wobble across a step sends 10 alternating updates in 11.5 hours and leaves 2 of
# STACK_DAILY_CAP for the real fall that follows. Ratcheting fixes that: the report only moves one
# way, so a monotone fill costs at most len(DISK_STEPS) updates and every one of them is news.
DISK_STEPS = (1, 0.5, 0.25, 0.1)

Http = Callable[..., str]
Run = Callable[..., str]  # (argv, timeout=...) -> stdout; see run_cmd


@dataclass(frozen=True)
class Timing:
    """Spacing that protects the ntfy budget and the phone from noise."""
    problem_age: timedelta  # a problem must span this long, as well as CONSECUTIVE_RUNS runs, to alert
    stack_gap: timedelta  # least time between stack updates, unless a new problem appears
    heartbeat_step: timedelta  # the scheduled "unreachable" is moved at most once per step
    heartbeat_slop: timedelta  # timer jitter tolerated when deciding a step has passed
    min_grace: timedelta


REAL_TIMING = Timing(problem_age=timedelta(minutes=10), stack_gap=timedelta(hours=1),
                     heartbeat_step=timedelta(hours=1), heartbeat_slop=timedelta(minutes=2),
                     min_grace=timedelta(minutes=30))
# --test: no spacing, so an alert can be forced with back-to-back runs and a short grace.
TEST_TIMING = Timing(problem_age=timedelta(0), stack_gap=timedelta(0), heartbeat_step=timedelta(0),
                     heartbeat_slop=timedelta(0), min_grace=timedelta(seconds=10))


class WatchError(Exception):
    """An error whose message this script wrote, so it is safe to put in a notification."""


# --------------------------------------------------------------------------- config & plumbing


def parse_env_file(text: str) -> dict[str, str]:
    env = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        quoted = re.fullmatch(r"""(['"])(.*)\1(\s+#.*)?""", value)
        value = quoted[2] if quoted else re.split(r"\s+#", value, maxsplit=1)[0]
        env[key.strip()] = value
    return env


def load_config(stack_dir: Path, environ) -> dict[str, str]:
    env_file = stack_dir / ".env"
    cfg = parse_env_file(env_file.read_text()) if env_file.exists() else {}
    cfg.update(environ)
    return cfg


def parse_duration(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", text)
    if not match:
        raise ValueError(f"invalid duration {text!r}: use a number and s/m/h/d, e.g. 90m or 24h")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def parse_size(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([MGT])\s*", text, re.IGNORECASE)
    if not match:
        raise ValueError(f"invalid size {text!r}: use a number and M/G/T, e.g. 100G")
    return int(float(match[1]) * SIZE_UNITS[match[2].upper()])


def size(nbytes: float, precision: str = ".10g") -> str:
    """The largest whole unit, in parse_size's format, or plain bytes below a megabyte (which
    DISK_FREE_MIN's 1G floor and DISK_STEPS put out of reach, but which is not parse_size's format
    and is not meant to be fed back to it).

    The default keeps enough digits never to round a threshold or a step into a number the drive was
    not actually under. `precision` is for the one figure here that is measured rather than derived:
    free space arrives with every digit the filesystem has, and 142.5914421G is what that looks like
    in a notification. MEASURED is that precision, and its 4 significant digits are also the most
    that can render without an exponent, since a value large enough for 5 digits in one unit is
    already past the next one."""
    for unit, scale in (("T", SIZE_UNITS["T"]), ("G", SIZE_UNITS["G"]), ("M", SIZE_UNITS["M"])):
        if nbytes >= scale:
            return f"{nbytes / scale:{precision}}{unit}"
    return f"{int(nbytes)}B"


MEASURED = ".4g"  # see size(): for free space, which is read off the filesystem, not computed


def span(delta: timedelta) -> str:
    """The largest whole unit, in parse_duration's format."""
    seconds = int(delta.total_seconds())
    for unit, scale in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= scale and seconds % scale == 0:
            return f"{seconds // scale}{unit}"
    return f"{seconds}s"


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def when(moment: datetime) -> str:
    local = moment.astimezone()
    return f"{local:%a} {local.day} {local:%b %H:%M}"


def brief(error) -> str:
    """One line of at most MAX_DETAIL_CHARS."""
    text = " ".join(str(error).split())
    return text if len(text) <= MAX_DETAIL_CHARS else text[:MAX_DETAIL_CHARS - 1] + "…"


def describe_failure(exc: BaseException) -> str:
    """Text this script wrote, or the error's type and errno meaning; never the error's message."""
    if isinstance(exc, WatchError):
        return brief(exc)
    if isinstance(exc, OSError) and exc.errno:
        return f"{type(exc).__name__} ({os.strerror(exc.errno)})"
    return type(exc).__name__


def sequence_safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text)


def give_up_after(seconds: float, what: str, call: Callable[[], object]):
    """call()'s result or exception, or TimeoutError once `seconds` have passed.

    call() runs on a thread that is abandoned, not cancelled, when time runs out: a thread stuck in a
    syscall can't be cancelled. That is safe only because it is a daemon thread, which doesn't hold
    up the interpreter's exit; a non-daemon one is joined at shutdown, which is the very overrun
    this exists to prevent. TimeoutError is an OSError, so it arrives wherever the call's own
    failures are already handled. `what` goes into its message, so it must never name a path or a
    URL: the topic is public, and both can carry it or the user's home directory."""
    outcome: dict = {}

    def attempt() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
            outcome["error"] = exc

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(seconds)
    if "error" in outcome:
        raise outcome["error"]
    if "value" not in outcome:
        raise TimeoutError(f"{what} took longer than {seconds}s")
    return outcome["value"]


def http_request(method: str, url: str, headers: dict | None = None, data: bytes | None = None,
                 timeout: float = 20) -> str:
    """The body of the reply, in at most `timeout` seconds.

    urlopen's own timeout is not that: it bounds each socket operation, so a reply trickling in
    (measured: 3s under a 0.5s timeout) or a name slow to resolve runs on past it. The unit's time
    limit is checked against the sum of these timeouts (RunTimeBudgetTest), so each has to be a real
    ceiling. A request given up on keeps going for the rest of the run and may still reach the server
    before the process exits. A reply that timed out already left that uncertainty; the window is
    now the rest of the run rather than one request."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    urlopen = urllib.request.urlopen  # looked up now, not whenever the thread gets to run

    def fetch() -> str:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    return give_up_after(timeout, "the request", fetch)


def run_cmd(argv: list, timeout: float = 60) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"{argv[0]} exited {proc.returncode}")
    return proc.stdout


def load_state(path: Path, now: datetime, dry_run: bool) -> tuple[dict, Path | None]:
    """Returns (state, where an unreadable file was moved). A file that isn't a JSON object (power lost
    mid-write, a bad hand edit) is moved aside, so the watch starts fresh instead of failing forever."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, None
    except ValueError:  # JSONDecodeError and UnicodeDecodeError
        data = None
    if isinstance(data, dict):
        return data, None
    aside = path.with_name(f"{path.name}.corrupt-{now.astimezone(timezone.utc):%Y%m%d%H%M%S}")
    if not dry_run:
        path.replace(aside)
    return {}, aside


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as file:
        file.write(json.dumps(data, indent=2, sort_keys=True))
        file.flush()
        os.fsync(file.fileno())
    tmp.replace(path)


@dataclass
class Notifier:
    server: str
    topic: str
    http: Http
    title_prefix: str = ""
    dry_run: bool = False

    def send(self, title: str, message: str, *, priority: int = HIGH, tags: tuple = (),
             sequence_id: str | None = None, delay_until: datetime | None = None) -> None:
        # JSON publishing, because header values cannot carry non-Latin-1 text.
        body = {"topic": self.topic, "title": self.title_prefix + title, "message": message,
                "priority": priority}
        if tags:
            body["tags"] = list(tags)
        if sequence_id:
            body["sequence_id"] = sequence_id
        if delay_until:
            body["delay"] = str(int(delay_until.timestamp()))
        if self.dry_run:
            print(json.dumps({k: v for k, v in body.items() if k != "topic"}, indent=2))
            return
        self.http("POST", self.server.rstrip("/") + "/", {"Content-Type": "application/json"},
                  json.dumps(body).encode())

    def delete(self, sequence_id: str) -> None:
        """Deletes a sequence; for a scheduled message, that cancels it before delivery."""
        if self.dry_run:
            print(f"DELETE sequence {sequence_id}")
            return
        topic = urllib.parse.quote(self.topic, safe="")
        self.http("DELETE", f"{self.server.rstrip('/')}/{topic}/{sequence_id}", {}, None)

    def feed(self, query: str) -> list[dict] | None:
        """One poll of the topic, or None when the server can't be read."""
        topic = urllib.parse.quote(self.topic, safe="")
        try:
            body = self.http("GET", f"{self.server.rstrip('/')}/{topic}/json?{query}", {}, None)
            return [event for event in (json.loads(line) for line in body.splitlines() if line.strip())
                    if isinstance(event, dict)]
        except (OSError, ValueError, HTTPException) as exc:
            # describe_failure, not the error: a URLError quotes the URL back, and the URL is the
            # topic. Anyone who knows the topic can read and cancel on it.
            print(f"can't read the topic: {describe_failure(exc)}", file=sys.stderr)
            return None

    def pending(self, sequence_id: str) -> bool | None:
        """Whether a message for this sequence is still waiting to be delivered, or None when the
        server can't say.

        A cancel ntfy accepted and one it ignored both answer 200, so the only way to tell them
        apart is to ask what is still scheduled. The trap is that `scheduled=1` does not mean "only
        the scheduled ones": measured against ntfy.sh, the plain poll returns the topic's delivered
        history and `scheduled=1` *adds* the ones still waiting. Asking the single question and
        matching on the sequence therefore matches every heartbeat this host ever delivered, and
        reports an alert as armed forever — which is what the first live run of this code did, on a
        cancel that had in fact worked on its first attempt.

        So the answer is the difference between the two feeds, by message id: what `scheduled=1`
        knows about and the delivered history does not is exactly what has yet to go out. Ids, not
        timestamps, so nothing here depends on this host's clock agreeing with ntfy's."""
        delivered = self.feed("poll=1&since=all")
        waiting = self.feed("poll=1&scheduled=1&since=all")
        if delivered is None or waiting is None:
            return None

        def messages(events):
            return {event.get("id") for event in events
                    if event.get("sequence_id") == sequence_id and event.get("event") == "message"}

        return bool(messages(waiting) - messages(delivered))


@dataclass
class Watch:
    """What a command runs with."""
    cfg: dict
    notifier: Notifier
    now: datetime
    http: Http
    run: Run
    host: str
    ident: str  # the host, made safe for sequence IDs, plus -test under --test
    timing: Timing = REAL_TIMING

    def sequence(self, kind: str) -> str:
        return f"{self.ident}-{kind}"


# --------------------------------------------------------------------------- stalled


def xml_tag(xml: str, tag: str) -> str:
    match = re.search(rf"<{tag}>([^<]*)</{tag}>", xml)
    return match[1] if match else ""


@dataclass
class Arr:
    name: str
    base: str
    key: str
    http: Http

    @classmethod
    def from_config(cls, cfg: dict, name: str, http: Http) -> "Arr":
        root = Path(cfg.get("CONFIG_ROOT") or "")
        if not root.is_absolute():  # as Compose does; systemd would resolve it against $HOME
            root = Path(cfg["STACK_DIR"]) / root
        try:
            xml = (root / "config" / name / "config.xml").read_text()
        except OSError as exc:
            raise WatchError(f"{name}: can't read its config.xml under CONFIG_ROOT") from exc
        base = cfg.get(f"{name.upper()}_URL") or (
            f"http://localhost:{xml_tag(xml, 'Port')}{xml_tag(xml, 'UrlBase')}")
        return cls(name, base.rstrip("/"), xml_tag(xml, "ApiKey"), http)

    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode(
            {k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()})
        try:
            return json.loads(self.http("GET", f"{self.base}/api/v3/{path}?{query}",
                                        {"X-Api-Key": self.key}))
        except urllib.error.HTTPError as exc:
            raise WatchError(f"{self.name}: HTTP {exc.code}") from exc
        except (OSError, ValueError, HTTPException) as exc:
            # URLError and timeouts; a body that isn't JSON; a reply cut short by an arr that is
            # restarting, which is an HTTPException and so is none of the other two.
            raise WatchError(f"{self.name}: no usable answer from its API") from exc

    def all_records(self, path: str, **params) -> list:
        records, page = [], 1
        while True:
            body = self.get(path, page=page, pageSize=250, **params)
            records.extend(body["records"])
            if not body["records"] or len(records) >= body["totalRecords"]:
                return records
            page += 1


@dataclass(frozen=True)
class Stalled:
    key: str
    group: str
    detail: str
    since: datetime


def available_since(movie: dict) -> datetime | None:
    """When the movie reached its minimum availability, as far as Radarr's dates say."""
    minimum = movie.get("minimumAvailability")
    if minimum == "inCinemas":
        return parse_ts(movie.get("inCinemas"))
    if minimum == "released":
        if movie.get("releaseDate"):
            return parse_ts(movie["releaseDate"])
        dates = [d for d in (parse_ts(movie.get("digitalRelease")),
                             parse_ts(movie.get("physicalRelease"))) if d]
        return min(dates) if dates else None
    return None


def stalled_movies(movies: list, queued_ids: set, now: datetime, stall_days: float) -> list[Stalled]:
    limit = timedelta(days=stall_days)
    out = []
    for movie in movies:
        if (not movie.get("monitored") or movie.get("hasFile") or not movie.get("isAvailable")
                or movie["id"] in queued_ids):
            continue
        since = max((d for d in (parse_ts(movie.get("added")), available_since(movie)) if d),
                    default=None)
        if since is not None and now - since > limit:
            out.append(Stalled(f"radarr:{movie['id']}", f"{movie['title']} ({movie['year']})", "",
                               since))
    return out


def stalled_episodes(episodes: list, queued_ids: set, now: datetime,
                     stall_days: float) -> list[Stalled]:
    limit = timedelta(days=stall_days)
    out = []
    for episode in episodes:
        aired = parse_ts(episode.get("airDateUtc"))
        if (not episode.get("monitored") or episode.get("hasFile") or aired is None
                or episode["id"] in queued_ids):
            continue
        series = episode.get("series") or {}
        since = max(d for d in (aired, parse_ts(series.get("added"))) if d)
        if now - since > limit:
            out.append(Stalled(
                f"sonarr:{episode['id']}",
                series.get("title") or f"Series {episode['seriesId']}",
                f"S{episode['seasonNumber']:02d}E{episode['episodeNumber']:02d}", since))
    return out


def digest_due(items: list[Stalled], notified: dict, now: datetime, remind_days: float,
               reminders: int) -> bool:
    """Due when an item is new, or is owed one of its `reminders` reminders."""
    remind = timedelta(days=remind_days)
    for item in items:
        entry = notified.get(item.key)
        if entry is None:
            return True
        if entry["sent"] <= reminders and now - parse_ts(entry["at"]) >= remind:
            return True
    return False


def format_digest(items: list[Stalled], notified: dict, stall_days: float) -> tuple[str, str]:
    groups: dict[str, list[Stalled]] = {}
    for item in sorted(items, key=lambda i: (i.group.lower(), i.detail)):
        groups.setdefault(item.group, []).append(item)
    new_lines, old_lines = [], []
    for group, members in groups.items():
        episodes = [m.detail for m in members if m.detail]
        if len(episodes) > MAX_EPISODES_LISTED:
            label = f"{group}: {len(episodes)} episodes"
        elif episodes:
            label = f"{group}: {', '.join(episodes)}"
        else:
            label = group
        is_new = any(m.key not in notified for m in members)
        line = f"• {label}, waiting since {when(min(m.since for m in members))}"
        (new_lines if is_new else old_lines).append(line + " (new)" if is_new else line)
    count = len(groups)
    title = f"{count} wanted title{'s' if count != 1 else ''} not downloaded after {stall_days:g} days"
    footer = ("\n\nUsually no release exists at the allowed quality yet, or the indexer is not "
              "returning results. Details: Sonarr/Radarr > Wanted > Missing. Unmonitor a title "
              "there to stop hearing about it.")
    # New titles first, so the title that triggered this digest is never the one cut off.
    return title, fit_lines(new_lines + old_lines, footer)


def fit_lines(lines: list[str], footer: str) -> str:
    """As many lines as fit in one ntfy message ahead of the footer, then "… and N more"."""
    budget = MAX_MESSAGE_BYTES - len(footer.encode())
    used = shown = 0
    for line in lines:
        hidden_after = len(lines) - shown - 1
        more = len(f"\n… and {hidden_after} more".encode()) if hidden_after else 0
        cost = len(line.encode()) + (1 if shown else 0)
        if used + cost + more > budget:
            break
        used, shown = used + cost, shown + 1
    hidden = len(lines) - shown
    return "\n".join(lines[:shown] + ([f"… and {hidden} more"] if hidden else [])) + footer


def arr_is_off(cfg: dict, name: str) -> bool:
    """Whether this arr is switched off, the same way JELLYFIN_URL=off switches Jellyfin off.

    Without an off switch a stack that doesn't run one of the arrs — a fork with no Sonarr, or one
    of them stopped for a while — gets a daily "Stack watch is failing: <name>: can't read its
    config.xml", with nothing the artifex can set to stop it short of turning the whole digest off.
    The missing file is a real error for a service that is supposed to be there, so the setting is
    what distinguishes the two; the script cannot tell them apart on its own."""
    return (cfg.get(f"{name.upper()}_URL") or "").strip().lower() == "off"


def run_stalled(watch: Watch, state: dict) -> None:
    cfg, now = watch.cfg, watch.now
    stall_days = float(cfg.get("STALL_DAYS") or 3)
    remind_days = float(cfg.get("STALL_REMIND_DAYS") or 7)
    items: list[Stalled] = []
    if not arr_is_off(cfg, "radarr"):
        radarr = Arr.from_config(cfg, "radarr", watch.http)
        queued = {r.get("movieId") for r in radarr.all_records("queue")}
        items += stalled_movies(radarr.all_records("wanted/missing", monitored=True), queued, now,
                                stall_days)
    if not arr_is_off(cfg, "sonarr"):
        sonarr = Arr.from_config(cfg, "sonarr", watch.http)
        queued = {r.get("episodeId") for r in sonarr.all_records("queue")}
        items += stalled_episodes(
            sonarr.all_records("wanted/missing", monitored=True, includeSeries=True), queued, now,
            stall_days)

    # Before bd9252e an entry was just the time it was first sent.
    notified = {key: {"at": entry, "sent": 1} if isinstance(entry, str) else entry
                for key, entry in state.get("notified", {}).items()}
    if items and digest_due(items, notified, now, remind_days, STALL_REMINDERS):
        title, message = format_digest(items, notified, stall_days)
        watch.notifier.send(title, message, priority=DEFAULT, tags=("hourglass_flowing_sand",))
        notified = {item.key: {"at": now.isoformat(),
                               "sent": notified.get(item.key, {}).get("sent", 0) + 1}
                    for item in items}
    else:
        notified = {item.key: notified[item.key] for item in items if item.key in notified}
    state["notified"] = notified


# --------------------------------------------------------------------------- check


COMMAND_ERRORS = (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError)
BLINDING_PROBLEMS = {"docker", "compose"}  # while either is up, services' states are unknown

# Seerr is built, not pulled, so that its download tracker stops issuing an arr write that freezes the
# progress bar (images/seerr/README.md). Its healthcheck passes identically without that patch, so the
# patch is asserted by reading the file the build edits. The service, and its container_name.
SEERR_CONTAINER = "seerr"
SEERR_TRACKER = "/app/dist/lib/downloadtracker.js"  # patch-downloadtracker.sh's target
SEERR_PATCH = "seerr:patch"
# The read takes ~0.2s. run_cmd's 60 would spend 55s more of the run's time budget on it.
SEERR_EXEC_TIMEOUT = 5


def stack_problems(stack_dir: Path, run: Run) -> dict[str, str]:
    try:
        config = json.loads(run(["docker", "compose", "--project-directory", str(stack_dir),
                                 "config", "--format", "json"]))
    except COMMAND_ERRORS as exc:
        print(f"docker compose config failed: {exc}", file=sys.stderr)
        return {"compose": "compose: can't read the stack config (details in the journal)"}
    try:
        ids = run(["docker", "ps", "-aq", "--filter",
                   f"label=com.docker.compose.project={config['name']}"]).split()
        containers = json.loads(run(["docker", "inspect", *ids])) if ids else []
    except COMMAND_ERRORS as exc:
        print(f"docker failed: {exc}", file=sys.stderr)
        return {"docker": "docker: not responding (details in the journal)"}
    problems = container_problems(sorted(config["services"]), containers)
    # A Seerr that isn't running can't be read, and is already the problem being reported.
    if SEERR_CONTAINER in config["services"] and f"service:{SEERR_CONTAINER}" not in problems:
        problem = seerr_patch_problem(run)
        if problem:
            problems[SEERR_PATCH] = problem
    return problems


def seerr_patch_problem(run: Run) -> str | None:
    """Whether the running Seerr still lacks the refreshMonitoredDownloads call its build deletes.

    The file is read whole and searched here rather than with `grep -c`, which exits 1 precisely when
    the count is the 0 that means patched — indistinguishable, through run_cmd, from a failed exec.
    The patch is an absence, so the getQueue calls it leaves in place are what show this is the
    tracker at all and not an empty or unrelated file."""
    try:
        source = run(["docker", "exec", SEERR_CONTAINER, "cat", SEERR_TRACKER], timeout=SEERR_EXEC_TIMEOUT)
    except (*COMMAND_ERRORS, UnicodeDecodeError) as exc:  # run_cmd decodes stdout as text
        print(f"seerr patch check failed: {exc}", file=sys.stderr)
        return "seerr: can't check its download-tracker patch (details in the journal)"
    if "getQueue" not in source:
        print(f"seerr patch check: {SEERR_TRACKER} has no getQueue call ({len(source)} characters read)",
              file=sys.stderr)
        return "seerr: can't recognise its download tracker, so the patch is unchecked"
    if "refreshMonitoredDownloads" in source:
        return "seerr: running without its download-tracker patch (see images/seerr/README.md)"
    return None


def container_problems(services: list[str], containers: list[dict]) -> dict[str, str]:
    states: dict[str, list[dict]] = {}
    for container in containers:
        labels = container["Config"]["Labels"] or {}
        if labels.get("com.docker.compose.oneoff") == "True":
            continue
        states.setdefault(labels.get("com.docker.compose.service"), []).append(container["State"])
    problems = {}
    for service in services:
        found = states.get(service)
        if not found:
            problems[f"service:{service}"] = f"{service}: no container"
            continue
        healthy = [s for s in found if s["Status"] == "running"
                   and (s.get("Health") or {}).get("Status") != "unhealthy"]
        if healthy:
            continue
        state = found[0]
        if state["Status"] == "running":
            why = "unhealthy"
        elif state["Status"] == "exited":
            why = f"exited (code {state.get('ExitCode')})"
        else:
            why = state["Status"]
        problems[f"service:{service}"] = f"{service}: {why}"
    return problems


def jellyfin_problem(url: str, http: Http) -> str | None:
    try:
        body = http("GET", url.rstrip("/") + "/health", {}, None, 10).strip()
    except urllib.error.HTTPError as exc:
        return f"jellyfin: health check returned HTTP {exc.code}"
    except (OSError, HTTPException) as exc:  # URLError and timeouts; a truncated reply
        # http.client.HTTPException is not an OSError, and a Jellyfin that is restarting is exactly
        # what produces one: IncompleteRead or BadStatusLine from a connection dropped mid-reply.
        # Uncaught it leaves this function, ends the whole run, and after CONSECUTIVE_RUNS reports
        # "Stack watch is failing: IncompleteRead" — the watch blaming itself, and taking the
        # container and disk checks down with it, for the ordinary case it exists to report.
        # The reason is the OS's or OpenSSL's text and it quotes the URL back: a DNS failure names
        # the host, a certificate failure names it again. JELLYFIN_URL is a LAN address today and a
        # Tailscale name later, and the topic is public, so the reason goes to the journal only.
        print(f"jellyfin health check failed: {exc}", file=sys.stderr)
        return "jellyfin: not answering (details in the journal)"
    if body == "Healthy":
        return None
    if body in ("Degraded", "Unhealthy"):
        return f"jellyfin: reports {body}"
    return "jellyfin: unexpected answer from /health"  # not Jellyfin's words, so not forwarded


def disk_minimum(cfg: dict) -> int | None:
    """DISK_FREE_MIN in bytes, or None when the disk check is off. Blank means the default, as
    everywhere else; only "off" turns the check off."""
    text = (cfg.get("DISK_FREE_MIN") or "").strip() or DISK_FREE_MIN_DEFAULT
    if text.lower() == "off":
        return None
    try:
        minimum = parse_size(text)
    except ValueError as exc:
        raise WatchError("DISK_FREE_MIN must be a size like 100G, or off") from exc
    if minimum < SIZE_UNITS["G"]:
        raise WatchError("DISK_FREE_MIN must be at least 1G, or off")
    return minimum


def read_free(path: str) -> tuple[int, int, int]:
    stat = os.statvfs(path)
    return os.stat(path).st_dev, stat.f_bavail * stat.f_frsize, stat.f_blocks * stat.f_frsize


def filesystem_free(path: str, timeout: float = DISK_READ_TIMEOUT,
                    read=read_free) -> tuple[int, int, int]:
    """(which filesystem, bytes this user may still write, its total size) for the filesystem
    holding path.

    The read is given up on after `timeout` (give_up_after), because the calls it makes have no
    timeout of their own and a spun-down, failing or stale network mount blocks them
    uninterruptibly. Giving up raises TimeoutError, an OSError, so it arrives at disk_problems'
    unreadable-path branch as any other unreadable path does.

    Neither obvious alternative works. `subprocess.run(timeout=)` is not safer: on timeout it calls
    kill() and then, on POSIX, wait() with no timeout of its own, and a process wedged in D-state
    does not die on SIGKILL, so it can block in exactly the same way. `ThreadPoolExecutor` is worse — its
    workers are non-daemon and its atexit hook joins them, which is the hang this avoids.

    Without this the whole run is lost rather than one path: the disk check runs after the container
    and Jellyfin checks, so TimeoutStartSec would SIGTERM the unit with their results gathered but
    unsaved and no failure recorded, and nothing would be heard until the heartbeat said the host
    was asleep or offline. Migration-relevant: today's library is a local ext4 partition."""
    return give_up_after(timeout, "reading free space", lambda: read(path))


def disk_problems(cfg: dict, minimum: int, floors: dict, free_space=None,
                  free_now: dict | None = None) -> dict[str, str]:
    """One entry per filesystem behind DISK_ROLES with less than `minimum` free. Paths sharing a
    filesystem share an entry: on a single-drive host that is one alert, not one per role. A path
    that isn't configured isn't checked; one that can't be read is a problem in its own right, and
    is reported rather than raised, so the container and Jellyfin checks still run.

    Keyed per role, never per role set. The key used to be built from the roles sharing the
    filesystem, so `disk:library+downloads` retired and two unknown keys appeared the moment
    SABNZBD_TEMP moved to its own drive — the move docs/sabnzbd.md recommends. That read as a new
    problem for a drive that had been low for days, and both reports went back to the top step
    because the stored floor belonged to the key that had just gone. Only the wording is merged:
    roles sharing a filesystem share one sentence, and format_check lists distinct descriptions, so
    a single-drive host still sees one problem rather than two identical ones.

    `floors` maps a problem key to the lowest level already reported for it, in bytes, and is
    updated here; run_check keeps it for as long as the problem lasts. Reporting the low point
    rather than the moment is what makes the text stable (see DISK_STEPS), and is why it says
    "dropped below": free space coming back up past a step doesn't make that untrue, so there is
    nothing to republish. Bytes, not the DISK_STEPS fraction, because retuning DISK_FREE_MIN
    mid-problem would rescale a fraction into a level the drive was never actually under.

    `free_now`, when given, collects what each role's filesystem actually has free at this moment,
    for the notification to quote beside the ratcheted sentence. It is a separate dict, and neither
    part of `floors` nor of the description, on purpose: free space is the one quantity here that
    varies continuously, and letting it into `entry["description"]` is exactly the flap DISK_STEPS'
    ratchet exists to prevent. The notification quotes it; nothing ever compares it."""
    free_space = free_space or filesystem_free
    filesystems: dict[int, tuple[list[str], int]] = {}
    problems = {}
    for setting, role, fallback in DISK_ROLES:
        configured = (cfg.get(setting) or "").strip()
        path = configured or fallback
        if not path:
            continue
        try:
            device, free, total = free_space(path)
        except OSError as exc:
            if not configured and f"disk:{role}" not in floors:
                # Compose's fallback, which it only creates on `up`. Not the user's claim about
                # their disk, so an absent one is neither a problem nor worth a journal line every
                # fifteen minutes for as long as the setting stays blank.
                #
                # Only while it isn't already a problem, though. A floor exists for exactly the
                # roles that were a problem on the last run (run_check drops the rest), and once one
                # is, the path going quiet is not recovery: skipping it here lets advance() decay the
                # tracked problem into a genuine-looking all clear naming it as "Cleared", when all
                # that is actually known is that the path stopped answering. A missed alert dressed
                # as good news is the worst thing this check can do, so an unreadable path that was
                # low stays a problem — an unreadable one — until it can be read again.
                continue
            print(f"can't read free space for {setting}: {exc}", file=sys.stderr)
            problems[f"disk:{role}"] = f"disk: can't read free space for the {role} path"
            continue
        if not configured and total < minimum:
            # A filesystem smaller than the threshold can never satisfy it, so watching it would be a
            # standing alert nobody can clear — and it shares one ntfy sequence_id with the real
            # library-full alert, so it would train the artifex to swipe away the one notification
            # this whole check exists to deliver. Compose's fallback is usually /tmp, a tmpfs of a few
            # gigabytes against a threshold sized for the media library. A path the user configured is
            # their own claim about their disk and is left alone; this one the script inferred.
            #
            # Accepted residual, unlike the unreadable case above: dropping a fallback that IS
            # already a problem does send a false "Cleared". Holding it instead would be the standing
            # alert nobody can clear that this branch was written to remove, so the honest tradeoff
            # is the other way round here. It takes a deliberate act to reach — raising DISK_FREE_MIN
            # past the fallback's whole size, or moving the fallback onto a small filesystem — and
            # the journal line below says what happened. See docs/stack-watch.md.
            print(f"not watching {setting}'s fallback: {size(total)} filesystem can't hold "
                  f"{size(minimum)} free", file=sys.stderr)
            continue
        filesystems.setdefault(device, ([], free))[0].append(role)
    for device, (roles, free) in filesystems.items():
        if free >= minimum:
            continue
        step = min(fraction for fraction in DISK_STEPS if free < minimum * fraction)
        keys = [f"disk:{role}" for role in roles]
        # A floor belongs to a filesystem, not to a role, so it is stored with the device it was
        # measured on and ignored once that changes. Keyed by role alone it would follow a role onto
        # a different drive and keep reporting a level the new drive has never been under — silently
        # and for as long as the problem lasts, since an unchanged description republishes nothing.
        # That is the same falsehood the bytes-not-fractions choice below was made to avoid, arriving
        # by another route. A floor with no device at all is one this version inherited from the
        # last, and is honoured once before being stamped with the device it is really on.
        kept = [floor["level"] for key in keys
                if (floor := floors.get(key)) and floor.get("device") in (device, None)]
        # One floor for the filesystem, so roles that have just been merged onto it can't report two
        # different low points in two bullets for the same drive.
        level = min([int(minimum * step)] + kept)
        description = f"disk: dropped below {size(level)} free for the {' and '.join(roles)}"
        for key in keys:
            floors[key] = {"device": device, "level": level}
            problems[key] = description
            # Every disk bullet carries the figure, not only the ones the ratchet has fallen behind
            # on. A rule the reader can't see is worse than fifteen characters: told sometimes, he
            # has no way to know whether a bullet without a figure means "still there" or "this
            # build doesn't say", and the question the sentence raises — how much is there now? —
            # is the same one every time.
            if free_now is not None:
                free_now[key] = free
    return problems


def migrate_disk_keys(state: dict) -> None:
    """Rewrite state written when a disk key carried the whole set of roles sharing a filesystem
    (`disk:library+downloads`) into one key per role, and floors stored as a bare level into the
    {device, level} form.

    Without it the first run after this upgrade retires the key the artifex is already looking at and
    introduces two he has never seen, which publish_stack can only read as new problems: a
    rotating_light at the highest priority the system has, about a drive where nothing has happened.
    Worse, the stored floor goes with the retired key, so if free space has recovered within the
    episode the replacement is computed fresh at a *higher* step — the notification that arrives at
    top priority carries better news than the one it replaces. Good news dressed as an emergency is
    the bug 238199f was written to kill, reaching the same place from the other side.

    `shown` and `reported` are migrated too: leaving them behind would produce exactly the same
    key churn one layer down."""
    for holder in (state.get("disk"), state.get("tracked"),
                   state.get("stack", {}).get("shown"), state.get("stack", {}).get("reported")):
        if not isinstance(holder, dict):
            continue
        for key in [k for k in holder if k.startswith("disk:") and "+" in k]:
            value = holder.pop(key)
            for role in key.removeprefix("disk:").split("+"):
                holder.setdefault(f"disk:{role}", value)
    floors = state.get("disk")
    if isinstance(floors, dict):
        for key, level in floors.items():
            if not isinstance(level, dict):
                floors[key] = {"level": level}  # no device: honoured once, then stamped


def gather_problems(watch: Watch, floors: dict | None = None,
                    free_now: dict | None = None) -> dict[str, str]:
    problems = stack_problems(Path(watch.cfg["STACK_DIR"]), watch.run)
    jellyfin_url = watch.cfg.get("JELLYFIN_URL") or "http://localhost:8096"
    if jellyfin_url.strip().lower() != "off":
        problem = jellyfin_problem(jellyfin_url, watch.http)
        if problem:
            problems["jellyfin"] = problem
    minimum = disk_minimum(watch.cfg)
    if minimum is not None:
        problems |= disk_problems(watch.cfg, minimum, floors if floors is not None else {},
                                  free_now=free_now)
    return problems


def advance(tracked: dict, problems: dict[str, str], now: datetime, problem_age: timedelta) -> dict:
    """Counts sightings. A problem alerts once seen on CONSECUTIVE_RUNS runs, none stale, spanning at
    least problem_age (so a catch-up run at wake plus the timer tick seconds later is not enough). It
    stays alerted until absent on CONSECUTIVE_RUNS runs in a row."""
    updated = {}
    for key, description in problems.items():
        previous = tracked.get(key) or {}
        seen = parse_ts(previous.get("seen"))
        if previous.get("alerted") or (seen is not None and now - seen <= STALE_AFTER):
            count, alerted = previous["count"] + 1, previous["alerted"]
            first = parse_ts(previous.get("first")) or seen
        else:
            count, first, alerted = 1, now, False
        if not alerted and count >= CONSECUTIVE_RUNS and now - first >= problem_age:
            alerted = True
        updated[key] = {"count": count, "first": first.isoformat(), "seen": now.isoformat(),
                        "alerted": alerted, "description": description, "absent": 0}
    for key, previous in tracked.items():
        if key not in problems and previous.get("alerted"):
            absent = previous.get("absent", 0) + 1
            if absent < CONSECUTIVE_RUNS:
                updated[key] = previous | {"absent": absent}
    return updated


def cleared_lines(cleared) -> str:
    """The "Cleared" block, or "" when nothing recovered.

    Distinct sentences, not keys: two roles on one drive share one sentence, so listing per key
    would name the same drive twice.

    Nothing subtracts the problems still being reported, because a sentence cannot be in both lists.
    Every description here names its own key's subject — "sonarr: …", or the roles sharing a drive —
    and a disk sentence naming role R is only ever written in the same pass that puts disk:R in
    `problems`, so a sentence still current always has its key still current. Worth keeping true:
    "Cleared: x" printed above "• x" would be a flat contradiction."""
    gone = sorted(set(cleared))
    return "\n\nCleared:\n" + "\n".join(f"• {d}" for d in gone) if gone else ""


def format_check(current: dict[str, str], host: str, worsened: bool, cleared: list[str] = (),
                 repeat: bool = False, since: datetime | None = None,
                 free_now: dict | None = None) -> tuple[str, str, int]:
    """`cleared` names what recovered, and `repeat` marks a re-send of something already reported.

    A repeat says "still not resolved" rather than "unchanged", because it can go out to someone who
    has just changed a great deal. The disk report ratchets to the low point of the episode, so an
    artifex who frees 60G to go from 10G to 70G is still under DISK_FREE_MIN, still has the problem,
    and would read "unchanged" as a claim that his deletions did nothing. `free_now` is the other
    half of that: a sentence the ratchet has held since the low point reads as a claim about now, so
    each disk bullet quotes what the drive actually has, and that same 70G is stated rather than
    left to be inferred from a sentence about 10G.

    Distinct descriptions, not keys: the disk check keys per role but words roles that share a
    filesystem as one sentence, and a single-drive host must read as one problem, not two identical
    ones. Naming what cleared is the only acknowledgement a recovery gets — the disk report ratchets,
    so freeing 60G to go from 10G to 70G changes nothing until DISK_FREE_MIN itself is cleared, and
    "everything that was down is back up" never said what had been down. That has to happen while
    problems remain, too: a recovery announced only when the last one goes is a recovery the artifex
    hears about on the system's schedule rather than his own."""
    free_now = free_now or {}
    if not current:
        return (f"Media stack on {host}: all clear",
                "Everything that was down is back up." + cleared_lines(cleared), LOW)
    lines = sorted({description + (f" (now {size(free_now[key], MEASURED)} free)"
                                   if key in free_now else "")
                    for key, description in current.items()})
    title = f"Media stack on {host}: {len(lines)} problem{'s' if len(lines) != 1 else ''}"
    message = "\n".join(f"• {line}" for line in lines) + cleared_lines(cleared)
    if repeat:
        message += "\n\nStill not resolved."
        if since is not None:
            message += f" First seen {when(since)}."
    return title, message, HIGH if worsened else DEFAULT


def publish_stack(watch: Watch, tracked: dict, stack: dict, free_now: dict | None = None) -> dict:
    """Brings the "<host>-stack" notification in line with the alerted problems and returns the new
    stack state. Returns `stack` unchanged when the update is held back or fails to send, so the next
    run tries again."""
    now, day = watch.now, timedelta(days=1)
    current = {key: entry["description"] for key, entry in tracked.items() if entry["alerted"]}
    shown = stack.get("shown", {})
    # Kept for the daily cap's window or the re-send's, whichever is longer. Pruned at a day flat,
    # raising STACK_REPEAT above a day would do nothing at all: every entry would age out before the
    # comparison below could be false, so the re-send would fire daily whatever the constant said.
    sent = sorted(t for t in map(parse_ts, stack.get("sent", []))
                  if now - t < max(day, STACK_REPEAT))
    # An unchanged set of problems is re-sent every STACK_REPEAT, so a standing problem isn't a
    # single notification the artifex may have dismissed long ago. An empty `sent` means nothing has
    # gone out within either window.
    unchanged = current == shown
    # `advance` holds a problem alerted through one absent run, so `current` can still hold something
    # that has in fact just gone; re-sending "still not resolved" about it would be false, and the
    # all clear is one run behind it anyway.
    going = any(entry.get("absent") for key, entry in tracked.items() if key in current)
    # `sent` is pruned at exactly the re-send window, so an empty one already means "nothing has gone
    # out within STACK_REPEAT". Comparing against sent[-1] as well reads like the cadence but cannot
    # ever be the reason — that clause was dead code, and a mutation test is what proved it.
    if unchanged and not (current and not going and not sent):
        return stack
    reported = {k: t for k, t in ((k, parse_ts(v)) for k, v in stack.get("reported", {}).items())
                if now - t < day}
    # A new problem is worse, and so is one whose description changed: every description here moves
    # between bad states (restarting -> exited, a disk step down), never towards good. Without this a
    # disk step worsening — the ratchet's whole escalation path — went out with a white_check_mark.
    worsened = bool(current.keys() - shown.keys()) or any(
        current[key] != shown[key] for key in current.keys() & shown.keys())
    # A problem not reported in the last day goes out even when muted; a flapping one doesn't.
    fresh = bool(current.keys() - shown.keys() - reported.keys())
    if not worsened and sent and now - sent[-1] < watch.timing.stack_gap:
        return stack
    if len(sent) >= STACK_DAILY_CAP and not fresh:
        print(f"Stack update muted: {STACK_DAILY_CAP} sent in the last day", file=sys.stderr)
        return stack
    started = [parse_ts(entry.get("first")) for key, entry in tracked.items() if key in current]
    title, message, priority = format_check(
        current, watch.host, worsened, [d for k, d in shown.items() if k not in current],
        unchanged, min([t for t in started if t], default=None), free_now)
    if len(sent) >= STACK_DAILY_CAP - 1:
        message += (f"\n\nThis has changed {STACK_DAILY_CAP} times in a day, so updates are muted "
                    f"until {when(sent[len(sent) - STACK_DAILY_CAP + 1] + day)}, except new problems.")
    # The tag follows the state, not the transition: a notification still titled "N problems" must
    # not carry a check mark, which is how a worsening disk step once went out as good news.
    watch.notifier.send(title, message, priority=priority, sequence_id=watch.sequence("stack"),
                        tags=("white_check_mark",) if not current
                        else ("rotating_light",) if worsened else ("warning",))
    return {"shown": current, "sent": [t.isoformat() for t in sent] + [now.isoformat()],
            "reported": {k: t.isoformat() for k, t in reported.items()} | {k: now.isoformat() for k in current}}


def parse_grace(text: str, timing: Timing) -> timedelta:
    # The alert is scheduled a step past grace, and DELAY_MARGIN keeps that off ntfy's hard limit.
    longest = MAX_DELAY - timing.heartbeat_step - DELAY_MARGIN
    try:
        grace = timedelta(seconds=parse_duration(text))
    except ValueError:
        grace = None
    if grace is None or not timing.min_grace <= grace <= longest:
        raise WatchError(f"HEARTBEAT_GRACE must be {span(timing.min_grace)} to {span(longest)}, or off")
    return grace


def heartbeat(watch: Watch, state: dict) -> None:
    """Keeps a scheduled "unreachable" message (ntfy delay) ahead of the last check-in. It is moved
    only when fewer than grace + slop remain before it goes out, to grace + step from now, so it costs
    one message per step at any grace and never goes out before grace has passed since a check-in
    whose reschedule succeeded (a failed one leaves the earlier message and its due in place).
    state["heartbeat"]["due"] is when the message goes out; comparing now to it (rather than to when it
    was published) keeps a changed HEARTBEAT_GRACE from sending a false "back online"."""
    now, timing, notifier = watch.now, watch.timing, watch.notifier
    sequence_id = watch.sequence("heartbeat")
    beat = state.setdefault("heartbeat", {})
    grace_text = (watch.cfg.get("HEARTBEAT_GRACE") or "24h").strip()
    if grace_text.lower() == "off":
        if beat.get("due") or beat.get("armed"):
            notifier.delete(sequence_id)
        del state["heartbeat"]
        return
    grace = parse_grace(grace_text, timing)
    if "armed" in beat:  # before this fix, the publish time was stored instead
        # With the grace it was published under unknown, assume at least the old 24h default, so
        # lowering the grace in the same edit can't make a delivered alert out of one that wasn't.
        beat["due"] = (parse_ts(beat.pop("armed")) + max(grace, timedelta(hours=24))).isoformat()
    due, last = parse_ts(beat.get("due")), parse_ts(beat.get("last"))
    if due is not None and now > due - CANCEL_SETTLE:
        # A run landing within CANCEL_SETTLE of `due` cannot know which way the race went: ntfy's
        # sender may already have released the message, or may be about to. Both branches below
        # publish on this sequence, and a publish replaces a message still waiting, so treating the
        # window as "not yet due" would let the reschedule quietly swallow an alert the artifex may
        # already be holding — an "unreachable" with no all clear ever coming, which is the one
        # outcome here nobody can act on. It is claimed as a recovery instead, and the sentence
        # stops short of asserting a delivery that is genuinely unknown.
        went_out = (f"The unreachable alert went out {when(due)}." if now > due else
                    f"The unreachable alert was due {when(due)} and may already have gone out.")
        notifier.send(f"{watch.host} is back online",
                      f"Out of contact from {when(last or due - grace)} to {when(now)}. {went_out}",
                      priority=DEFAULT, tags=("white_check_mark",), sequence_id=sequence_id)
        del beat["due"]  # announced, so not again even if rescheduling below fails
        due = None
    beat["last"] = now.isoformat()
    step, slop = timing.heartbeat_step, timing.heartbeat_slop
    if due is None or not grace + slop < due - now <= grace + step + slop:
        later = f" or up to {span(step)} later" if step else ""
        notifier.send(
            f"{watch.host} is unreachable",
            f"Its last check-in was {when(now)}{later}. It is asleep, powered off or offline, or the "
            "stack watch stopped (systemctl --user list-timers 'media-stack-*'). Jellyfin and Seerr "
            "can't be reached until it's back.",
            priority=HIGH, tags=("warning",), sequence_id=sequence_id, delay_until=now + grace + step)
        beat["due"] = (now + grace + step).isoformat()
        # When, not whether: `disarm` needs this to know if it is inside the second in which ntfy
        # ignores a cancel. Written only after the publish returns, so it dates a message that
        # really is on the server.
        beat["published"] = now.isoformat()


def run_check(watch: Watch, state: dict) -> None:
    errors = []
    migrate_disk_keys(state)
    previous = state.get("tracked", {})
    floors = state.setdefault("disk", {})
    # Measured this run and thrown away with it. Nothing here is saved: a figure read last run is
    # exactly the stale number this exists to replace, so an absent one has to mean "not measured
    # now", not "here is the last one I had".
    free_now: dict[str, int] = {}
    try:
        problems = gather_problems(watch, floors, free_now)
    except Exception as exc:  # still reschedule the heartbeat, or a false "unreachable" goes out
        errors.append(exc)
    else:
        tracked = advance(previous, problems, watch.now, watch.timing.problem_age)
        if problems.keys() & BLINDING_PROBLEMS:
            # Docker can't say how the services are, so keep what it last said rather than read its
            # silence as recovery.
            tracked |= {k: v for k, v in previous.items() if k.startswith("service:") or k == SEERR_PATCH}
        elif f"service:{SEERR_CONTAINER}" in problems and SEERR_PATCH in previous:
            # Nor is a stopped Seerr evidence that it has been rebuilt.
            tracked[SEERR_PATCH] = previous[SEERR_PATCH]
        # State from before the stack record: treat what was alerted then as already shown. Dated
        # now, because when it actually went out is unknowable here and STACK_REPEAT would otherwise
        # re-send every old alert on the first run after an upgrade — but only when there is
        # something to have sent, so a first install doesn't spend a slot of STACK_DAILY_CAP on a
        # notification that never existed.
        inherited = {k: v["description"] for k, v in previous.items() if v.get("alerted")}
        state.setdefault("stack", {"shown": inherited})
        # Dated now, because when it actually went out is unknowable here and STACK_REPEAT would
        # otherwise re-send every inherited alert on the first run after an upgrade — but only when
        # there is something to have sent, so a first install doesn't spend a slot of
        # STACK_DAILY_CAP on a notification that never existed. Also reached by a stack record
        # written before "sent" existed, which is what the previous upgrade path left behind.
        state["stack"].setdefault(
            "sent", [watch.now.isoformat()] if state["stack"].get("shown") else [])
        state["tracked"] = tracked
        # A floor lives exactly as long as its problem, so one run back above a step doesn't reset
        # it but a real all clear does. advance() has already applied the absence damping.
        state["disk"] = {key: level for key, level in floors.items() if key in tracked}
        try:
            state["stack"] = publish_stack(watch, tracked, state["stack"], free_now)
        except Exception as exc:
            errors.append(exc)
    try:
        heartbeat(watch, state)
    except Exception as exc:
        errors.append(exc)
    for exc in errors[1:]:
        traceback.print_exception(exc)
    if errors:
        raise errors[0]


# --------------------------------------------------------------------------- entry point


def record_failure(watch: Watch, command: str, state: dict, exc: Exception) -> None:
    failure = state.get("failure") or {}
    count = failure.get("count", 0) + 1
    since = parse_ts(failure.get("since")) or watch.now
    alerted = parse_ts(failure.get("alerted"))
    state["failure"] = failure = {"count": count, "since": since.isoformat()}
    if alerted:
        failure["alerted"] = alerted.isoformat()
    if count < CONSECUTIVE_RUNS or (alerted and watch.now - alerted < FAILURE_REPEAT):
        return
    try:
        watch.notifier.send(
            f"Stack watch on {watch.host} is failing",
            f"'{command}' has failed {count} runs in a row, since {when(since)}: {describe_failure(exc)}. "
            "Its alerts are off until this is fixed. Details: journalctl --user -u 'media-stack-*'",
            priority=HIGH, tags=("warning",), sequence_id=watch.sequence(f"watch-{command}"))
        failure["alerted"] = watch.now.isoformat()
    except Exception:
        traceback.print_exc()


def record_success(watch: Watch, command: str, state: dict) -> None:
    failure = state.pop("failure", None)
    if not (failure and failure.get("alerted")):
        return
    try:
        watch.notifier.send(f"Stack watch on {watch.host} is working again", f"'{command}' ran cleanly.",
                            priority=LOW, tags=("white_check_mark",),
                            sequence_id=watch.sequence(f"watch-{command}"))
    except Exception:
        traceback.print_exc()
        state["failure"] = {"alerted": failure["alerted"]}  # say so next time


def run_disarm(watch: Watch, state_path: Path, dry_run: bool = False, sleep=time.sleep) -> int:
    """Cancels the dead man's switch and confirms the server really dropped it.

    ntfy answers 200 to a cancel it ignores and to one it accepts alike, so the old single DELETE
    could not tell "cancelled" from "still armed" — and it is ignored when it arrives in the same
    second as the publish, which is precisely when the two callers that matter run it: the
    installer cancelling the heartbeat a failed first install had just scheduled, and --uninstall
    landing just after a timer tick. What was left armed then fires hours later, telling the artifex
    a host he deliberately shut down is unreachable.

    So: wait out the rest of that second when the state file says one was just published, cancel,
    then ask what is still scheduled and cancel again if it is. The wait is only an optimisation —
    it saves a doomed attempt — and the confirmation is what actually decides, which is what keeps
    this correct when `published` is absent, stale or wrong.

    Exit code 1 means the alert is known to be still armed, or the cancel itself could not be sent;
    a cancel that went out but could not be confirmed is reported and treated as success, because it
    almost certainly worked and the caller's alternative is to fail a clean uninstall."""
    sequence_id = watch.sequence("heartbeat")
    if dry_run:
        watch.notifier.delete(sequence_id)
        return 0
    # Read-only, and never fatal: an unreadable state file only costs the wait below its head start.
    # The cancel has to go out even then — losing state is a reason to disarm, not a reason not to.
    state: dict = {}
    try:
        state, _ = load_state(state_path, watch.now, dry_run=True)
    except OSError as exc:
        print(f"can't read {state_path}, so the cancel can't be timed: {exc}", file=sys.stderr)
    beat = state.get("heartbeat")
    published = beat.get("published") if isinstance(beat, dict) else None
    # `beat` itself may be anything a broken state file holds, so nothing above this line assumes a
    # shape; the cancel matters more than the state that describes it.
    settle = CANCEL_SETTLE.total_seconds()
    try:
        # Only the part of the blind spot that is left: one published an hour ago needs no wait.
        pause = settle - (watch.now - parse_ts(published)).total_seconds() if published else 0
    except (AttributeError, TypeError, ValueError):
        # A hand-edited or half-written timestamp must not stop a disarm; it only means the wait
        # cannot be shortened, and the confirmation below is what decides either way.
        pause = settle
    if 0 < pause <= settle:
        sleep(pause)
    armed = None
    for attempt in range(CANCEL_ATTEMPTS):
        if attempt:
            sleep(settle)
        try:
            watch.notifier.delete(sequence_id)
        except Exception as exc:
            print(f"can't cancel the scheduled alert: {describe_failure(exc)}", file=sys.stderr)
            return 1
        armed = watch.notifier.pending(sequence_id)
        if armed is not True:
            break
    if armed:
        print(f"the scheduled alert is still armed after {CANCEL_ATTEMPTS} attempts: it will go out "
              "when its delay expires. Cancel it by hand, or ignore the alert when it arrives.",
              file=sys.stderr)
        return 1
    if armed is None:
        print("cancelled, but ntfy could not confirm it (details above)", file=sys.stderr)
    try:
        if state.pop("heartbeat", None) is not None:
            save_json(state_path, state)
    except OSError as exc:
        print(f"Cancelled, but can't update {state_path}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv=None, environ=None, http: Http = http_request, run: Run = run_cmd,
         now: datetime | None = None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["check", "stalled", "disarm"])
    parser.add_argument("--test", action="store_true",
                        help="prefix titles with TEST:, use separate state and sequence IDs, and drop "
                             "the spacing between alerts, to force one without disturbing the real ones")
    parser.add_argument("--dry-run", action="store_true",
                        help="print notifications instead of sending them; state is not saved")
    args = parser.parse_args(argv)

    environ = os.environ if environ is None else environ
    stack_dir = Path(environ.get("STACK_DIR") or STACK_DIR)
    cfg = load_config(stack_dir, environ)
    cfg["STACK_DIR"] = str(stack_dir)
    if not cfg.get("NTFY_TOPIC"):
        print("NTFY_TOPIC is not set in .env or the environment", file=sys.stderr)
        return 2
    suffix = "-test" if args.test else ""
    host = cfg.get("WATCH_HOSTNAME") or socket.gethostname()
    notifier = Notifier(cfg.get("NTFY_SERVER") or "https://ntfy.sh", cfg["NTFY_TOPIC"], http,
                        "TEST: " if args.test else "", args.dry_run)
    watch = Watch(cfg, notifier, now or datetime.now(timezone.utc), http, run, host,
                  sequence_safe(host) + suffix, TEST_TIMING if args.test else REAL_TIMING)
    state_home = Path(cfg.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    state_name = "stalled" if args.command == "stalled" else "check"
    state_path = Path(cfg.get("STATE_DIR") or state_home / "media-stack-watch") / f"{state_name}{suffix}.json"

    if args.command == "disarm":
        return run_disarm(watch, state_path, args.dry_run, sleep)

    try:
        state, moved_to = load_state(state_path, watch.now, args.dry_run)
        if not args.dry_run:
            # Prove state can be saved before sending anything. If it can't (a full disk), every run
            # would repeat the same alerts and reschedules and flood the shared ntfy budget. Sending
            # nothing instead lets the scheduled "unreachable" go out once grace has passed.
            save_json(state_path, state)
    except OSError as exc:
        print(f"Can't load or save {state_path}, so nothing was sent: {exc}", file=sys.stderr)
        return 1
    if moved_to:
        try:
            notifier.send(f"Stack watch on {host} started over",
                          f"{state_path.name} was unreadable, so it was moved aside to {moved_to.name} and "
                          "the watch started fresh. An alert it already sent may repeat.",
                          priority=DEFAULT, tags=("warning",))
        except Exception:
            traceback.print_exc()

    if "failures" in state:  # the counter's shape before 2026-09-15's fixes
        state["failure"] = {"count": state.pop("failures"), "since": watch.now.isoformat()}
        if state.pop("failure_alerted", False):
            state["failure"]["alerted"] = watch.now.isoformat()
    runner = run_check if args.command == "check" else run_stalled
    try:
        runner(watch, state)
    except Exception as exc:
        traceback.print_exc()
        record_failure(watch, args.command, state, exc)
        code = 1
    else:
        record_success(watch, args.command, state)
        code = 0
    if not args.dry_run:
        try:
            save_json(state_path, state)
        except OSError as exc:
            # The probe above proved the file could be written before anything was sent, so getting
            # here means the disk filled during the run. Everything this run decided is lost: the
            # alerts it just sent are not recorded as shown, the heartbeat's new `due` is not
            # recorded as scheduled, and the failure counter that would eventually say so cannot be
            # incremented either. The next run starts from the same state and sends the same things
            # again, held down only by STACK_DAILY_CAP, which is a cap and not a fix.
            #
            # Nothing is notified from here on purpose: a notification would be the one thing that
            # does repeat every run, since suppressing it needs the state that cannot be written.
            # The journal and a non-zero exit — which systemd records — are what is left.
            print(f"Can't save {state_path}, so this run's alerts and heartbeat are not recorded "
                  f"and may repeat: {exc}", file=sys.stderr)
            return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
