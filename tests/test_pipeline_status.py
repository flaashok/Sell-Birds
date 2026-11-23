"""Whether the background loop is running, waiting or off, and each stage's state.

The loop notes its phase and next wake-up (sellbird.loop_status) so Today can
say "Waiting, next run at 3:40" rather than a run log that only ever says
"completed"; each job it runs notes when it ran and how it went. Discover's
tiles show a run started in a terminal too, and when an hourly search
allowance frees up.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

import sellbird.dashboard as dash
from sellbird import loop_status as ls
from sellbird.collectors import discover
from sellbird.state import StateManager
from tests.test_dashboard_live_status import _company, _sql, _ts, client, env  # noqa: F401  (fixtures)

NOW = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)


class Store:
    def __init__(self):
        self.data = {}

    async def get_setting(self, key):
        return self.data.get(key, "")

    async def set_setting(self, key, value):
        self.data[key] = value


# ── The loop's own notes ──


def test_the_loop_notes_when_it_wakes_next():
    store = Store()
    asyncio.run(ls.note_loop(store, ls.WORKING, action="write_campaign", interval_minutes=15, now=NOW))
    asyncio.run(ls.note_loop(store, ls.SLEEPING, seconds=900, cycle_done=True,
                             interval_minutes=15, now=NOW + timedelta(minutes=2)))
    loop = asyncio.run(ls.read_loop(store))
    assert loop["phase"] == "sleeping"
    assert loop["next_at"] == "2026-11-18T15:17:00Z"
    assert loop["cycle_started_at"] == "2026-11-18T15:00:00Z"
    assert loop["last_cycle_at"] == "2026-11-18T15:02:00Z"


def test_a_note_that_fails_never_stops_the_loop():
    class Broken:
        async def get_setting(self, key):
            raise OSError("disk full")

        async def set_setting(self, key, value):
            raise OSError("disk full")

    asyncio.run(ls.note_loop(Broken(), ls.SLEEPING, seconds=60))
    assert asyncio.run(ls.timed(Broken(), "profile", _value(5))) == 5


async def _value(v):
    return v


async def _boom():
    raise RuntimeError("site timed out")


def test_each_job_notes_how_it_went():
    store = Store()
    assert asyncio.run(ls.timed(store, "profile", _value(3))) == 3
    rec = asyncio.run(ls.read_stage(store, "profile"))
    assert rec["ok"] and not rec["running"] and rec["runs_today"] == 1 and rec["errors_today"] == 0

    with pytest.raises(RuntimeError):
        asyncio.run(ls.timed(store, "profile", _boom()))
    rec = asyncio.run(ls.read_stage(store, "profile"))
    assert not rec["ok"] and "site timed out" in rec["error"]
    assert rec["runs_today"] == 2 and rec["errors_today"] == 1


def test_replies_that_couldnt_be_read_count_as_an_error():
    store = Store()
    assert asyncio.run(ls.timed(store, "handle_replies", _value(False))) is False
    rec = asyncio.run(ls.read_stage(store, "handle_replies"))
    assert rec["errors_today"] == 1 and "Replies" in rec["error"]


def test_sending_is_one_stage_whichever_job_sent():
    today = "2026-11-18"
    merged = ls.merge_stages([
        {"day": today, "last_started": "2026-11-18T10:00:00Z", "runs_today": 2, "errors_today": 1},
        {"day": today, "last_started": "2026-11-18T12:00:00Z", "runs_today": 1, "ok": True},
        {},
    ], today)
    assert merged["last_started"] == "2026-11-18T12:00:00Z"
    assert merged["runs_today"] == 3 and merged["errors_today"] == 1


# ── Running now, waiting or off ──


def test_off_says_how_to_start_it():
    view = ls.scheduler_view({"phase": "sleeping", "since": "2026-11-18T14:00:00Z"},
                             running=False, now=NOW)
    assert view["state"] == "off" and "sellbird run" in view["reason"]


def test_waiting_says_until_when_and_why():
    loop = {"phase": "sleeping", "since": "2026-11-18T14:55:00Z",
            "next_at": "2026-11-18T15:10:00Z", "interval_minutes": 15}
    view = ls.scheduler_view(loop, running=True, now=NOW)
    assert view["state"] == "waiting" and view["next_at"] == "2026-11-18T15:10:00Z"
    assert "every 15 minutes" in view["reason"] and view["tone"] == "waiting"


def test_running_now_names_the_job():
    loop = {"phase": "working", "since": "2026-11-18T14:59:00Z", "action": "write_campaign"}
    view = ls.scheduler_view(loop, running=True, now=NOW)
    assert view["state"] == "running_now" and view["reason"].startswith("Writing")


def test_a_paused_loop_says_why():
    loop = {"phase": "paused", "since": "2026-11-18T14:59:00Z", "next_at": "2026-11-18T15:14:00Z",
            "note": "Paused: Codex isn't signed in."}
    view = ls.scheduler_view(loop, running=True, now=NOW)
    assert view["state"] == "waiting" and view["tone"] == "bad" and "signed in" in view["reason"]


def test_a_note_from_an_earlier_process_is_not_this_ones():
    loop = {"phase": "sleeping", "since": "2026-11-18T09:00:00Z", "next_at": "2026-11-18T09:15:00Z"}
    view = ls.scheduler_view(loop, running=True, started_at=NOW - timedelta(minutes=1), now=NOW)
    assert view["state"] == "running_now" and view["reason"] == "Starting up."


def test_overdue_wake_up_is_said_plainly():
    loop = {"phase": "sleeping", "since": "2026-11-18T14:00:00Z", "next_at": "2026-11-18T14:15:00Z"}
    view = ls.scheduler_view(loop, running=True, now=NOW)
    assert view["reason"].startswith("Due now")


def test_a_loop_that_never_reported_is_not_responding():
    view = ls.scheduler_view({}, running=True, started_at=NOW - timedelta(days=5), now=NOW)
    assert view["state"] == "stuck" and "5 days" in view["reason"]
    fresh = ls.scheduler_view({}, running=True, started_at=NOW - timedelta(minutes=3), now=NOW)
    assert fresh["reason"] == "Starting up."


def test_a_loop_days_past_its_wake_up_is_not_responding():
    # A loop that holds the lock but can't write its notes (its folder was
    # moved while it ran) must not go on saying "Due now" for days.
    loop = {"phase": "error", "since": "2026-11-13T14:45:00Z", "next_at": "2026-11-13T15:00:00Z"}
    view = ls.scheduler_view(loop, running=True, now=NOW)
    assert view["state"] == "stuck" and view["tone"] == "bad"
    assert "5 days" in view["reason"] and "start it again" in view["reason"]


# ── /api/pipeline ──


def _stage(data, key):
    return next(s for s in data["stages"] if s["key"] == key)


def test_pipeline_when_the_loop_is_off(client, env):
    data = client.get("/api/pipeline").json()
    assert data["scheduler"]["state"] == "off"
    assert [s["key"] for s in data["stages"]] == [
        "find", "read", "emails", "verify", "write", "approve", "send"]
    assert all(s["state"] in ("off", "manual", "clear") for s in data["stages"])
    assert _stage(data, "read")["next_note"] == "When Sellbird runs."


def _loop_running(monkeypatch, env, loop):
    monkeypatch.setattr(dash, "_check_sellbird_pid", lambda: 4242)
    monkeypatch.setattr(dash, "_loop_started_at", lambda pid: datetime.now(UTC) - timedelta(hours=1))

    async def _note():
        await StateManager(db_path=str(env["db"])).set_setting(ls.LOOP_KEY, json.dumps(loop))
    asyncio.run(_note())


def test_pipeline_waiting_shows_the_next_run_on_each_stage(client, env, monkeypatch):
    nxt = (datetime.now(UTC) + timedelta(minutes=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _loop_running(monkeypatch, env, {
        "phase": "sleeping", "since": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "next_at": nxt, "interval_minutes": 15})
    _company(env, "a", "a.test")            # one site not read yet
    data = client.get("/api/pipeline").json()
    assert data["scheduler"]["state"] == "waiting" and data["scheduler"]["next_at"] == nxt
    read = _stage(data, "read")
    assert read["state"] == "scheduled" and read["next"] == nxt and read["waiting"] == 1
    assert _stage(data, "find")["state"] == "manual"


def test_pipeline_counts_what_waits_for_approval(client, env):
    _sql(env, "INSERT INTO prospects (id, email, status) VALUES ('p', 'pat@example.com', 'new')")
    for oid in ("o1", "o2"):
        _sql(env, "INSERT INTO outbox (id, prospect_id, to_email, subject, body, status, kind, step) "
                  "VALUES (?, 'p', 'pat@example.com', 's', 'b', 'pending_review', 'sequence', 1)", (oid,))
    approve = _stage(client.get("/api/pipeline").json(), "approve")
    assert approve["state"] == "your_turn" and approve["waiting"] == 2


def test_a_job_that_failed_today_shows_its_error(client, env, monkeypatch):
    _loop_running(monkeypatch, env, {"phase": "sleeping",
                                     "since": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
    state = StateManager(db_path=str(env["db"]))
    with pytest.raises(RuntimeError):
        asyncio.run(ls.timed(state, "write_campaign", _boom()))
    write = _stage(client.get("/api/pipeline").json(), "write")
    assert write["last_result"] == "error" and "site timed out" in write["last_note"]
    assert write["errors_today"] == 1


# ── Discover's tiles ──


def test_a_run_started_in_a_terminal_shows_as_running(client, env):
    _sql(env, "INSERT INTO runs (id, stage, status, provider, params_json, started_at) "
              "VALUES ('r1', 'discover', 'running', 'tinyfish', '{\"queries\": 12}', ?)", (_ts(3),))
    _company(env, "a", "a.test")
    _sql(env, "INSERT INTO observations (company_id, signal_code, collector, observed_at, run_id) "
              "VALUES ('a', 'TECH_STACK', 'discover', ?, 'r1')", (_ts(1),))
    data = client.get("/api/discover/summary").json()
    assert data["current"]["from"] == "terminal" and data["current"]["provider"] == "tinyfish"
    assert data["current"]["found"] == 1 and data["current"]["queries"] == 12
    assert data["found_total"] == 1


def test_the_last_run_says_how_it_ended(client, env):
    _sql(env, "INSERT INTO runs (id, stage, status, provider, records, error, started_at, ended_at) "
              "VALUES ('r0', 'discover', 'failed', 'serper', 7, 'Out of credits', ?, ?)",
         (_ts(90), _ts(80)))
    data = client.get("/api/discover/summary").json()
    assert data["current"] is None
    assert data["last"]["status"] == "failed" and data["last"]["found"] == 7
    assert data["last"]["error"] == "Out of credits"


def test_the_hourly_allowance_resets_an_hour_after_its_first_search(client, env, monkeypatch):
    async def _pick():
        await StateManager(db_path=str(env["db"])).set_setting("discovery_provider", "tinyfish")
    asyncio.run(_pick())
    now = datetime.now(UTC).timestamp()
    discover.note_search("tinyfish", now=now - 1800)
    discover.note_search("tinyfish", now=now - 60)
    limit = client.get("/api/discover/summary").json()["limit"]
    assert limit["limited"] and limit["used"] == 2 and limit["per_hour"] == 500
    reset = datetime.fromisoformat(limit["resets_at"].replace("Z", "+00:00")).timestamp()
    assert abs(reset - (now + 1800)) < 2


def test_searches_older_than_an_hour_are_dropped(tmp_path):
    log = tmp_path / "s.json"
    discover.note_search("tinyfish", path=log, now=1000.0)
    discover.note_search("tinyfish", path=log, now=5000.0)
    assert discover.searches_this_hour("tinyfish", path=log, now=5001.0) == [5000.0]
