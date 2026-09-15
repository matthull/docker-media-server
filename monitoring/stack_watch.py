#!/usr/bin/env python3
"""Stack watch: the alerts this stack's services cannot send about themselves.

  check    Every CHECK_INTERVAL. Alerts when a Compose service is stopped or unhealthy, Jellyfin
           stops answering, the drive the library and downloads live on is running out of room,
           or Docker itself is down. Also keeps an ntfy dead man's switch scheduled that fires if
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
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
MAX_DELAY = timedelta(days=3)  # ntfy.sh's message-delay-limit
STALL_REMINDERS = 2  # reminders about a stalled title after the digest that first listed it
MAX_EPISODES_LISTED = 4  # a series missing more episodes than this is shown as a count
MAX_MESSAGE_BYTES = 4096  # ntfy delivers anything longer as an attachment
MAX_DETAIL_CHARS = 160  # longest library-written error detail quoted in a notification
LOW, DEFAULT, HIGH = 2, 3, 4  # ntfy priorities
SIZE_UNITS = {"M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
# The settings whose filesystems the disk check watches, and what an alert calls each one. Both are
# checked separately because either can be moved to its own drive. The word, never the path, is what
# goes out: the topic is public and the paths name the user's home directory.
DISK_ROLES = (("MEDIA_ROOT", "library"), ("SABNZBD_TEMP", "downloads"))
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
Run = Callable[[list], str]


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


def size(nbytes: float) -> str:
    """The largest whole unit, in parse_size's format."""
    for unit, scale in (("T", SIZE_UNITS["T"]), ("G", SIZE_UNITS["G"]), ("M", SIZE_UNITS["M"])):
        if nbytes >= scale:
            return f"{nbytes / scale:.10g}{unit}"
    return f"{int(nbytes)}B"


def span(delta: timedelta) -> str:
    """The largest whole unit, in parse_duration's format."""
    seconds = int(delta.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
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


def http_request(method: str, url: str, headers: dict | None = None, data: bytes | None = None,
                 timeout: float = 20) -> str:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


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
        except (OSError, ValueError) as exc:  # URLError and timeouts; a body that isn't JSON
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


def run_stalled(watch: Watch, state: dict) -> None:
    cfg, now = watch.cfg, watch.now
    stall_days = float(cfg.get("STALL_DAYS") or 3)
    remind_days = float(cfg.get("STALL_REMIND_DAYS") or 7)
    radarr = Arr.from_config(cfg, "radarr", watch.http)
    queued = {r.get("movieId") for r in radarr.all_records("queue")}
    items = stalled_movies(radarr.all_records("wanted/missing", monitored=True), queued, now, stall_days)
    sonarr = Arr.from_config(cfg, "sonarr", watch.http)
    queued = {r.get("episodeId") for r in sonarr.all_records("queue")}
    items += stalled_episodes(sonarr.all_records("wanted/missing", monitored=True, includeSeries=True),
                              queued, now, stall_days)

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
    return container_problems(sorted(config["services"]), containers)


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
    except OSError as exc:  # URLError and timeouts; their reason is the OS's text, not the server's
        return f"jellyfin: not answering ({brief(getattr(exc, 'reason', exc))})"
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


def filesystem_free(path: str) -> tuple[int, int]:
    """(which filesystem, bytes on it this user may still write) for the filesystem holding path."""
    stat = os.statvfs(path)
    return os.stat(path).st_dev, stat.f_bavail * stat.f_frsize


def disk_problems(cfg: dict, minimum: int, floors: dict, free_space=None) -> dict[str, str]:
    """One entry per filesystem behind DISK_ROLES with less than `minimum` free. Paths sharing a
    filesystem share an entry: on a single-drive host that is one alert, not one per role. A path
    that isn't configured isn't checked; one that can't be read is a problem in its own right, and
    is reported rather than raised, so the container and Jellyfin checks still run.

    `floors` maps a problem key to the lowest level already reported for it, in bytes, and is
    updated here; run_check keeps it for as long as the problem lasts. Reporting the low point
    rather than the moment is what makes the text stable (see DISK_STEPS), and is why it says
    "dropped below": free space coming back up past a step doesn't make that untrue, so there is
    nothing to republish. Bytes, not the DISK_STEPS fraction, because retuning DISK_FREE_MIN
    mid-problem would rescale a fraction into a level the drive was never actually under."""
    free_space = free_space or filesystem_free
    filesystems: dict[int, tuple[list[str], int]] = {}
    problems = {}
    for key, role in DISK_ROLES:
        path = (cfg.get(key) or "").strip()
        if not path:
            continue
        try:
            device, free = free_space(path)
        except OSError as exc:
            print(f"can't read free space for {key}: {exc}", file=sys.stderr)
            problems[f"disk:{role}"] = f"disk: can't read free space for the {role} path"
            continue
        filesystems.setdefault(device, ([], free))[0].append(role)
    for roles, free in filesystems.values():
        if free >= minimum:
            continue
        step = min(fraction for fraction in DISK_STEPS if free < minimum * fraction)
        key = f"disk:{'+'.join(roles)}"
        reached = int(minimum * step)
        level = floors[key] = min(reached, floors.get(key, reached))
        problems[key] = (
            f"disk: dropped below {size(level)} free for the {' and '.join(roles)}")
    return problems


def gather_problems(watch: Watch, floors: dict | None = None) -> dict[str, str]:
    problems = stack_problems(Path(watch.cfg["STACK_DIR"]), watch.run)
    jellyfin_url = watch.cfg.get("JELLYFIN_URL") or "http://localhost:8096"
    if jellyfin_url.strip().lower() != "off":
        problem = jellyfin_problem(jellyfin_url, watch.http)
        if problem:
            problems["jellyfin"] = problem
    minimum = disk_minimum(watch.cfg)
    if minimum is not None:
        problems |= disk_problems(watch.cfg, minimum, floors if floors is not None else {})
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


def format_check(current: dict[str, str], host: str, worsened: bool) -> tuple[str, str, int]:
    if not current:
        return f"Media stack on {host}: all clear", "Everything that was down is back up.", LOW
    count = len(current)
    title = f"Media stack on {host}: {count} problem{'s' if count != 1 else ''}"
    return title, "\n".join(f"• {d}" for d in sorted(current.values())), HIGH if worsened else DEFAULT


def publish_stack(watch: Watch, tracked: dict, stack: dict) -> dict:
    """Brings the "<host>-stack" notification in line with the alerted problems and returns the new
    stack state. Returns `stack` unchanged when the update is held back or fails to send, so the next
    run tries again."""
    now, day = watch.now, timedelta(days=1)
    current = {key: entry["description"] for key, entry in tracked.items() if entry["alerted"]}
    shown = stack.get("shown", {})
    if current == shown:
        return stack
    sent = sorted(t for t in map(parse_ts, stack.get("sent", [])) if now - t < day)
    reported = {k: t for k, t in ((k, parse_ts(v)) for k, v in stack.get("reported", {}).items())
                if now - t < day}
    worsened = bool(current.keys() - shown.keys())
    # A problem not reported in the last day goes out even when muted; a flapping one doesn't.
    fresh = bool(current.keys() - shown.keys() - reported.keys())
    if not worsened and sent and now - sent[-1] < watch.timing.stack_gap:
        return stack
    if len(sent) >= STACK_DAILY_CAP and not fresh:
        print(f"Stack update muted: {STACK_DAILY_CAP} sent in the last day", file=sys.stderr)
        return stack
    title, message, priority = format_check(current, watch.host, worsened)
    if len(sent) >= STACK_DAILY_CAP - 1:
        message += (f"\n\nThis has changed {STACK_DAILY_CAP} times in a day, so updates are muted "
                    f"until {when(sent[len(sent) - STACK_DAILY_CAP + 1] + day)}, except new problems.")
    watch.notifier.send(title, message, priority=priority, sequence_id=watch.sequence("stack"),
                        tags=("rotating_light",) if worsened else ("white_check_mark",))
    return {"shown": current, "sent": [t.isoformat() for t in sent] + [now.isoformat()],
            "reported": {k: t.isoformat() for k, t in reported.items()} | {k: now.isoformat() for k in current}}


def parse_grace(text: str, timing: Timing) -> timedelta:
    longest = MAX_DELAY - timing.heartbeat_step  # the alert is scheduled a step past grace
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
    one message per step at any grace and never goes out before grace has passed since a check-in.
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
    if due is not None and now > due:
        notifier.send(f"{watch.host} is back online",
                      f"Out of contact from {when(last or due - grace)} to {when(now)}. "
                      f"The unreachable alert went out {when(due)}.",
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


def run_check(watch: Watch, state: dict) -> None:
    errors = []
    previous = state.get("tracked", {})
    floors = state.setdefault("disk", {})
    try:
        problems = gather_problems(watch, floors)
    except Exception as exc:  # still reschedule the heartbeat, or a false "unreachable" goes out
        errors.append(exc)
    else:
        tracked = advance(previous, problems, watch.now, watch.timing.problem_age)
        if problems.keys() & BLINDING_PROBLEMS:
            # Docker can't say how the services are, so keep what it last said rather than read its
            # silence as recovery.
            tracked |= {k: v for k, v in previous.items() if k.startswith("service:")}
        # State from before the stack record: treat what was alerted then as already shown.
        state.setdefault("stack", {"shown": {k: v["description"] for k, v in previous.items()
                                             if v.get("alerted")}})
        state["tracked"] = tracked
        # A floor lives exactly as long as its problem, so one run back above a step doesn't reset
        # it but a real all clear does. advance() has already applied the absence damping.
        state["disk"] = {key: level for key, level in floors.items() if key in tracked}
        try:
            state["stack"] = publish_stack(watch, tracked, state["stack"])
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


def main(argv=None, environ=None, http: Http = http_request, run: Run = run_cmd,
         now: datetime | None = None) -> int:
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
        # Cancel first, so that a full or broken disk can't leave the alert scheduled.
        notifier.delete(watch.sequence("heartbeat"))
        if args.dry_run:
            return 0
        try:
            state, _ = load_state(state_path, watch.now, dry_run=True)
            if state.pop("heartbeat", None) is not None:
                save_json(state_path, state)
        except OSError as exc:
            print(f"Cancelled, but can't update {state_path}: {exc}", file=sys.stderr)
            return 1
        return 0

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
        save_json(state_path, state)
    return code


if __name__ == "__main__":
    sys.exit(main())
