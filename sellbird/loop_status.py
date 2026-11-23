"""What the background loop (`sellbird run`) is doing, where the dashboard can see it.

The loop writes two kinds of note to the settings table, both best-effort
(a failed write never stops the loop):

- ``loop_status``: its phase right now (working, or waiting and why) and when
  it next wakes, so Today can say "Running now", "Waiting, next run at 3:40"
  or "Off" instead of a run log that only ever says "completed".
- ``loop_stage:<task>``: for each job a cycle runs (reading websites, finding
  emails, writing, sending, reading replies), when it last started and ended,
  whether it went wrong, and how many runs and errors it had today.

Whether the loop is running at all comes from its PID file and lock, not from
these notes: a loop that was killed can't say so.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("sellbird")

LOOP_KEY = "loop_status"
STAGE_KEY = "loop_stage:{}"

WORKING = "working"
SLEEPING = "sleeping"
QUIET_HOURS = "quiet_hours"
USAGE_LIMIT = "usage_limit"
PAUSED = "paused"
ERROR = "error"
STOPPED = "stopped"

# How far past its wake-up time a running loop can be before the page says it
# isn't responding. Longer than the error backoff cap (15 min) and a laptop's
# wake-up catch-up, so a short sleep doesn't trip it.
STALE_AFTER = timedelta(hours=1)

# What each of the loop's jobs is called on the page.
TASK_LABELS = {
    "prospect": "Finding and checking emails",
    "write_campaign": "Writing",
    "send_campaign": "Sending",
    "send_outbox": "Sending",
    "handle_replies": "Reading replies",
    "profile": "Reading websites",
    "analyze": "Updating numbers",
    "quiet_hours_send": "Sending (quiet hours)",
    "idle": "Updating numbers",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if dt else ""


def _ago(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours >= 48:
        return f"{hours // 24} days"
    return "an hour" if hours <= 1 else f"{hours} hours"


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _read(state, key: str) -> dict:
    try:
        raw = await state.get_setting(key)
        data = json.loads(raw) if raw else {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def note_loop(state, phase: str, *, seconds: float | None = None, note: str = "",
                    action: str = "", interval_minutes: int | None = None,
                    cycle_done: bool = False, now: datetime | None = None) -> None:
    """Record the loop's phase. ``seconds`` is how long until it wakes."""
    now = now or _now()
    try:
        data = await _read(state, LOOP_KEY)
        if phase == WORKING:
            data["cycle_started_at"] = _iso(now)
        if cycle_done:
            data["last_cycle_at"] = _iso(now)
        data.update({
            "phase": phase,
            "since": _iso(now),
            "next_at": _iso(now + timedelta(seconds=seconds)) if seconds is not None else "",
            "note": note,
            "action": action,
        })
        if interval_minutes is not None:
            data["interval_minutes"] = interval_minutes
        await state.set_setting(LOOP_KEY, json.dumps(data))
    except Exception as e:
        logger.debug(f"Couldn't note the loop's status: {e}")


async def read_loop(state) -> dict:
    return await _read(state, LOOP_KEY)


def _failure(name: str, result) -> str:
    """Why a job's outcome counts as a failure, or "" when it doesn't."""
    if isinstance(result, BaseException):
        return f"{type(result).__name__}: {result}"[:300]
    if name == "handle_replies" and result is False:
        return "Replies couldn't all be read."
    return ""


async def _note_stage(state, name: str, *, started: datetime, ended: datetime | None,
                      error: str = "") -> None:
    try:
        key = STAGE_KEY.format(name)
        data = await _read(state, key)
        day = started.astimezone().date().isoformat()
        if data.get("day") != day:
            data.update(day=day, runs_today=0, errors_today=0)
        data["last_started"] = _iso(started)
        if ended is None:
            data["last_ended"] = ""
            data["running"] = True
        else:
            data["running"] = False
            data["last_ended"] = _iso(ended)
            data["ok"] = not error
            data["error"] = error
            data["runs_today"] = int(data.get("runs_today") or 0) + 1
            if error:
                data["errors_today"] = int(data.get("errors_today") or 0) + 1
                data["last_error_at"] = _iso(ended)
        await state.set_setting(key, json.dumps(data))
    except Exception as e:
        logger.debug(f"Couldn't note the {name} stage: {e}")


async def timed(state, name: str, coro):
    """Run one of the loop's jobs and note when it ran and how it went.
    The job's result or exception passes through unchanged."""
    started = _now()
    await _note_stage(state, name, started=started, ended=None)
    try:
        result = await coro
    except BaseException as e:   # a cancel (the cycle's time limit) is noted too
        msg = (_failure(name, e) if isinstance(e, Exception)
               else "Stopped before it finished (the cycle's time limit, or Sellbird stopping).")
        await _note_stage(state, name, started=started, ended=_now(), error=msg)
        raise
    await _note_stage(state, name, started=started, ended=_now(), error=_failure(name, result))
    return result


async def read_stage(state, name: str) -> dict:
    return await _read(state, STAGE_KEY.format(name))


def merge_stages(records: list[dict], today: str) -> dict:
    """One view of several loop jobs that make up one stage (Sending is
    send_outbox or send_campaign): the latest run, and today's counts summed."""
    records = [r for r in records if r]
    if not records:
        return {}
    latest = max(records, key=lambda r: r.get("last_started") or "")
    out = dict(latest)
    out["running"] = any(r.get("running") for r in records)
    out["runs_today"] = sum(int(r.get("runs_today") or 0) for r in records if r.get("day") == today)
    out["errors_today"] = sum(int(r.get("errors_today") or 0) for r in records if r.get("day") == today)
    return out


def scheduler_view(loop: dict, *, running: bool, started_at: datetime | None = None,
                   now: datetime | None = None) -> dict:
    """The one line at the top of the pipeline: Running now, Waiting (and
    until when, and why) or Off."""
    now = now or _now()
    interval = loop.get("interval_minutes")
    base = {"interval_minutes": interval, "last_cycle_at": loop.get("last_cycle_at", ""),
            "next_at": "", "reason": "", "tone": "idle"}
    if not running:
        return base | {
            "state": "off", "label": "Off",
            "reason": "Nothing runs on its own until you start it. Choose Start on Controls, "
                      "or run `sellbird run` in a terminal and leave it open.",
        }
    since = parse_iso(loop.get("since"))
    phase = loop.get("phase") or ""
    # No note yet, or one left by an earlier process: this one is starting.
    if not since or phase == STOPPED or (started_at and since < started_at - timedelta(seconds=5)):
        if started_at and now - started_at > STALE_AFTER:
            # Started long ago and never wrote a note: it can't reach its
            # database, or it's an old build that predates these notes.
            return base | {"state": "stuck", "label": "Not responding", "tone": "bad",
                           "reason": "It started " + _ago(now - started_at) + " ago and hasn't "
                                     "reported since, so it may not be sending. The log on "
                                     "Controls says why. Stop Sellbird and start it again."}
        return base | {"state": "running_now", "label": "Running now", "tone": "active",
                       "reason": "Starting up."}
    if phase == WORKING:
        what = TASK_LABELS.get(loop.get("action", ""), "")
        return base | {"state": "running_now", "label": "Running now", "tone": "active",
                       "since": loop.get("since", ""),
                       "reason": (what + ", and whatever else is due.") if what else "Working."}
    next_at = parse_iso(loop.get("next_at"))
    reasons = {
        SLEEPING: (f"Between runs. It runs every {interval} minutes." if interval
                   else "Between runs."),
        QUIET_HOURS: "Quiet hours. Nothing is found or written until they end.",
        USAGE_LIMIT: "The model's usage limit for today is reached. It checks again in an hour.",
        PAUSED: loop.get("note") or "Paused.",
        ERROR: "The last run hit an error, so it waits a little and tries again. "
               "The log on Controls says what went wrong.",
    }
    tone = "bad" if phase in (PAUSED, ERROR) else "waiting"
    view = base | {"state": "waiting", "label": "Waiting", "tone": tone,
                   "next_at": _iso(next_at) if next_at else "",
                   "reason": reasons.get(phase, "Between runs.")}
    if phase == QUIET_HOURS and loop.get("note"):
        view["reason"] = loop["note"]
    if next_at and now - next_at > STALE_AFTER:
        # Long past its wake-up time with no new note: the process is alive
        # (it holds the lock) but can't write here. Found in production when
        # the install folder was moved while it ran: it retried for days and
        # this line went on saying "Due now".
        return view | {"state": "stuck", "label": "Not responding", "tone": "bad",
                       "reason": "It was due to run " + _ago(now - next_at) + " ago and hasn't, so "
                                 "nothing is being sent. The log on Controls says why. Stop "
                                 "Sellbird and start it again."}
    if next_at and now - next_at > timedelta(minutes=2):
        # Past its wake-up time and no new note: the computer may have slept.
        view["reason"] = "Due now. If this stays, the computer may have been asleep."
    return view
