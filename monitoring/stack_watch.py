#!/usr/bin/env python3
"""Stack watch: the alerts this stack's services cannot send about themselves.

  check    Every 15 minutes. Alerts when a Compose service is stopped or unhealthy, Jellyfin
           stops answering, or Docker itself is down. Also keeps an ntfy dead man's switch
           armed that fires if this host stops checking in (asleep, powered off, offline).
  stalled  Daily. Alerts on monitored items Sonarr/Radarr still have no file for after
           STALL_DAYS. Nothing else reports this: a request can be accepted and then wait
           forever for a release that never comes.
  disarm   Cancels the dead man's switch. Needed whenever the timers stop for good, or the
           last scheduled "unreachable" alert still goes out.

Standard library only. Settings come from the stack's .env, with the process environment
taking precedence; API keys are read from each service's config.xml. See docs/stack-watch.md.
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
MAX_DELAY_SECONDS = 3 * 86400  # ntfy.sh message-delay-limit
MIN_DELAY_SECONDS = 10
# A problem, or a failing run of this script, must repeat on this many consecutive runs before it
# alerts. Absorbs container recreation on image updates and Docker still starting after boot.
CONSECUTIVE_RUNS = 2
# A sighting older than this does not count towards CONSECUTIVE_RUNS. Two 15-minute runs were missed,
# so the host slept or the timer stopped in between: the run before a suspend and the run after
# resume are not consecutive, and Docker is often still thawing in both.
STALE_AFTER = timedelta(minutes=40)
# ntfy.sh allows an anonymous IP 250 messages a day, shared with every service that alerts. Re-arming
# the heartbeat on every run would spend 96 of them, so it is re-armed only once its delivery time
# would move by this much, or by a 24th of the grace period when that is shorter.
REARM_MAX_STEP = timedelta(hours=1)
STALL_REMINDERS = 2
MAX_EPISODES_LISTED = 4
MAX_MESSAGE_BYTES = 4096  # ntfy delivers anything longer as an attachment
MAX_DETAIL_CHARS = 160

Http = Callable[..., str]
Run = Callable[[list], str]


# --------------------------------------------------------------------------- config & plumbing


def parse_env_file(text: str) -> dict[str, str]:
    env = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0]
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


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def when(moment: datetime) -> str:
    local = moment.astimezone()
    return f"{local:%a} {local.day} {local:%b %H:%M}"


def brief(error) -> str:
    """One line of at most MAX_DETAIL_CHARS, for error text that ends up in a notification."""
    text = " ".join(str(error).split())
    return text if len(text) <= MAX_DETAIL_CHARS else text[:MAX_DETAIL_CHARS - 1] + "…"


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


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


@dataclass
class Notifier:
    server: str
    topic: str
    http: Http
    title_prefix: str = ""
    dry_run: bool = False

    def send(self, title: str, message: str, *, priority: int = 4, tags: tuple = (),
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


# --------------------------------------------------------------------------- stalled


def xml_tag(xml: str, tag: str) -> str:
    match = re.search(rf"<{tag}>([^<]*)</{tag}>", xml)
    return match[1] if match else ""


@dataclass
class Arr:
    base: str
    key: str
    http: Http

    @classmethod
    def from_config(cls, cfg: dict, name: str, http: Http) -> "Arr | None":
        config_xml = Path(cfg["CONFIG_ROOT"]) / "config" / name / "config.xml"
        if not config_xml.exists():
            return None
        xml = config_xml.read_text()
        base = cfg.get(f"{name.upper()}_URL") or (
            f"http://localhost:{xml_tag(xml, 'Port')}{xml_tag(xml, 'UrlBase')}")
        return cls(base.rstrip("/"), xml_tag(xml, "ApiKey"), http)

    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode(
            {k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()})
        return json.loads(self.http("GET", f"{self.base}/api/v3/{path}?{query}",
                                    {"X-Api-Key": self.key}))

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
    lines = []
    for group, members in groups.items():
        episodes = [m.detail for m in members if m.detail]
        if len(episodes) > MAX_EPISODES_LISTED:
            label = f"{group}: {len(episodes)} episodes"
        elif episodes:
            label = f"{group}: {', '.join(episodes)}"
        else:
            label = group
        new = " (new)" if any(m.key not in notified for m in members) else ""
        lines.append(f"• {label}, waiting since {when(min(m.since for m in members))}{new}")
    count = len(groups)
    title = f"{count} wanted title{'s' if count != 1 else ''} not downloaded after {stall_days:g} days"
    footer = ("\n\nUsually no release exists at the allowed quality yet, or the indexer is not "
              "returning results. Details: Sonarr/Radarr > Wanted > Missing. Unmonitor a title "
              "there to stop hearing about it.")
    shown = len(lines)
    while True:
        hidden = len(lines) - shown
        message = "\n".join(lines[:shown]) + (f"\n… and {hidden} more" if hidden else "") + footer
        if len(message.encode()) <= MAX_MESSAGE_BYTES or shown == 0:
            return title, message
        shown -= 1


def run_stalled(cfg: dict, notifier: Notifier, state: dict, now: datetime, http: Http, run: Run,
                host: str, ident: str) -> None:
    stall_days = float(cfg.get("STALL_DAYS") or 3)
    remind_days = float(cfg.get("STALL_REMIND_DAYS") or 7)
    items: list[Stalled] = []
    radarr = Arr.from_config(cfg, "radarr", http)
    if radarr:
        queued = {r.get("movieId") for r in radarr.all_records("queue")}
        items += stalled_movies(radarr.all_records("wanted/missing", monitored=True), queued, now,
                                stall_days)
    sonarr = Arr.from_config(cfg, "sonarr", http)
    if sonarr:
        queued = {r.get("episodeId") for r in sonarr.all_records("queue")}
        items += stalled_episodes(
            sonarr.all_records("wanted/missing", monitored=True, includeSeries=True), queued, now,
            stall_days)

    notified = state.get("notified", {})
    if items and digest_due(items, notified, now, remind_days, STALL_REMINDERS):
        title, message = format_digest(items, notified, stall_days)
        notifier.send(title, message, priority=3, tags=("hourglass_flowing_sand",))
        notified = {item.key: {"at": now.isoformat(),
                               "sent": notified.get(item.key, {}).get("sent", 0) + 1}
                    for item in items}
    else:
        notified = {item.key: notified[item.key] for item in items if item.key in notified}
    state["notified"] = notified


# --------------------------------------------------------------------------- check


COMMAND_ERRORS = (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError)


def stack_problems(stack_dir: Path, run: Run) -> dict[str, str]:
    try:
        config = json.loads(run(["docker", "compose", "--project-directory", str(stack_dir),
                                 "config", "--format", "json"]))
    except COMMAND_ERRORS as exc:
        # Compose quotes .env values (credentials) in its errors, so details go to the journal only:
        # anything in a notification is readable by whoever knows the topic.
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
    except (urllib.error.URLError, OSError) as exc:
        return f"jellyfin: not answering ({brief(getattr(exc, 'reason', exc))})"
    return None if body == "Healthy" else f"jellyfin: reports {body[:60]!r}"


def advance(tracked: dict, problems: dict[str, str], now: datetime) -> tuple[dict, bool, bool]:
    """Returns (tracked, changed, worsened): changed when the set of alerted problems changed,
    worsened when that change includes a newly alerted problem."""
    updated, worsened = {}, False
    for key, description in problems.items():
        previous = tracked.get(key) or {}
        seen = parse_ts(previous.get("seen"))
        if previous.get("alerted") or (seen is not None and now - seen <= STALE_AFTER):
            count, alerted = previous["count"] + 1, previous["alerted"]
        else:
            count, alerted = 1, False
        entry = {"count": count, "alerted": alerted, "description": description,
                 "seen": now.isoformat()}
        if not alerted and count >= CONSECUTIVE_RUNS:
            entry["alerted"] = worsened = True
        updated[key] = entry
    recovered = any(v["alerted"] for k, v in tracked.items() if k not in problems)
    return updated, worsened or recovered, worsened


def format_check(tracked: dict, host: str, worsened: bool) -> tuple[str, str, int]:
    down = sorted(v["description"] for v in tracked.values() if v["alerted"])
    if not down:
        return f"Media stack on {host}: all clear", "Everything that was down is back up.", 2
    count = len(down)
    title = f"Media stack on {host}: {count} problem{'s' if count != 1 else ''}"
    return title, "\n".join(f"• {d}" for d in down), 4 if worsened else 3


def heartbeat(cfg: dict, notifier: Notifier, previous: dict, now: datetime, host: str,
              ident: str) -> dict:
    sequence_id = f"{ident}-heartbeat"
    grace_text = cfg.get("HEARTBEAT_GRACE") or "24h"
    if grace_text.strip().lower() == "off":
        if previous.get("armed"):
            notifier.delete(sequence_id)
        return {}
    grace = timedelta(seconds=parse_duration(grace_text))
    if not MIN_DELAY_SECONDS <= grace.total_seconds() <= MAX_DELAY_SECONDS:
        raise ValueError(f"HEARTBEAT_GRACE {grace_text!r} is outside ntfy's 10s to 3d delay limit")
    last, armed = parse_ts(previous.get("last")), parse_ts(previous.get("armed"))
    fired = armed is not None and now - armed > grace
    if fired:
        notifier.send(
            f"{host} is back online",
            f"Out of contact from {when(last or armed)} to {when(now)}. The unreachable alert went "
            f"out {when(armed + grace)}.",
            priority=3, tags=("white_check_mark",), sequence_id=sequence_id)
    if armed is None or fired or now - armed >= min(REARM_MAX_STEP, grace / 24):
        notifier.send(
            f"{host} is unreachable",
            f"It stopped checking in after {when(now)}, so it is asleep, powered off or offline. "
            "Jellyfin and Seerr can't be reached until it's back.",
            priority=4, tags=("warning",), sequence_id=sequence_id, delay_until=now + grace)
        armed = now
    return {"last": now.isoformat(), "armed": armed.isoformat()}


def run_check(cfg: dict, notifier: Notifier, state: dict, now: datetime, http: Http, run: Run,
              host: str, ident: str) -> None:
    problems = stack_problems(Path(cfg["STACK_DIR"]), run)
    jellyfin_url = cfg.get("JELLYFIN_URL") or "http://localhost:8096"
    if jellyfin_url.strip().lower() != "off":
        problem = jellyfin_problem(jellyfin_url, http)
        if problem:
            problems["jellyfin"] = problem

    errors = []
    tracked, changed, worsened = advance(state.get("tracked", {}), problems, now)
    try:
        if changed:
            title, message, priority = format_check(tracked, host, worsened)
            notifier.send(title, message, priority=priority, sequence_id=f"{ident}-stack",
                          tags=("rotating_light",) if worsened else ("white_check_mark",))
        state["tracked"] = tracked  # not advanced if the alert failed to send, so it retries
    except Exception as exc:
        errors.append(exc)
    try:
        state["heartbeat"] = heartbeat(cfg, notifier, state.get("heartbeat", {}), now, host, ident)
    except Exception as exc:
        errors.append(exc)
    if errors:
        raise errors[0]


# --------------------------------------------------------------------------- entry point


def main(argv=None, environ=None, http: Http = http_request, run: Run = run_cmd,
         now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["check", "stalled", "disarm"])
    parser.add_argument("--test", action="store_true",
                        help="prefix titles with TEST: and use separate state and sequence IDs, "
                             "to force an alert without disturbing the real ones")
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
    ident = sequence_safe(host) + suffix
    notifier = Notifier(cfg.get("NTFY_SERVER") or "https://ntfy.sh", cfg["NTFY_TOPIC"], http,
                        "TEST: " if args.test else "", args.dry_run)
    state_home = Path(cfg.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    state_name = "stalled" if args.command == "stalled" else "check"
    state_path = Path(cfg.get("STATE_DIR") or state_home / "media-stack-watch") / (
        f"{state_name}{suffix}.json")
    state = load_json(state_path)
    now = now or datetime.now(timezone.utc)

    if args.command == "disarm":
        notifier.delete(f"{ident}-heartbeat")
        if "heartbeat" in state and not args.dry_run:
            state.pop("heartbeat")
            save_json(state_path, state)
        return 0

    runner = run_check if args.command == "check" else run_stalled
    failure_id = f"{ident}-watch-{args.command}"
    try:
        runner(cfg, notifier, state, now, http, run, host, ident)
    except Exception as exc:
        traceback.print_exc()
        state["failures"] = state.get("failures", 0) + 1
        if state["failures"] >= CONSECUTIVE_RUNS and not state.get("failure_alerted"):
            try:
                notifier.send(
                    f"Stack watch on {host} is failing",
                    f"'{args.command}' failed {state['failures']} runs in a row: "
                    f"{type(exc).__name__}. The alerts it provides are off until this is fixed. "
                    "Details, kept out of notifications: journalctl --user -u 'media-stack-*'",
                    priority=4, tags=("warning",), sequence_id=failure_id)
                state["failure_alerted"] = True
            except Exception:
                traceback.print_exc()
        if not args.dry_run:
            save_json(state_path, state)
        return 1

    state.pop("failures", None)
    if state.get("failure_alerted"):
        try:
            notifier.send(f"Stack watch on {host} is working again", f"'{args.command}' ran cleanly.",
                          priority=2, tags=("white_check_mark",), sequence_id=failure_id)
            state.pop("failure_alerted")
        except Exception:
            traceback.print_exc()
    if not args.dry_run:
        save_json(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
