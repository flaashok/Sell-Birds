"""usage.quiet_hours.background_work: work that contacts no one runs at
night, while replies and sending keep to quiet hours."""
import asyncio

import pytest

from sellbird.config import SellbirdConfig
from tests.test_send_window import BASE, _patch_heartbeat, approved, seed_at, state  # noqa: F401


def _cfg(background=True, **email):
    usage = {"quiet_hours": {"background_work": background}}
    return SellbirdConfig(**BASE, usage=usage,
                          channels={"email": {"provider": "gmail", "send_window": {}, **email}})


async def _cycle(monkeypatch, cfg, state):
    calls, stop = [], asyncio.Event()
    main_mod = _patch_heartbeat(monkeypatch, cfg, state, calls, stop)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    return calls


@pytest.mark.asyncio
async def test_background_work_runs_at_night_but_nothing_is_sent(state, monkeypatch):
    calls = await _cycle(monkeypatch, _cfg(), state)
    assert "prospect" in calls
    assert "replies" not in calls and "send" not in calls


@pytest.mark.asyncio
async def test_cold_email_due_in_its_window_still_goes_after_replies(state, monkeypatch):
    pid = await seed_at(state, "Tokyo, Japan", "aiko@tokyo-acme.jp")
    await approved(state, pid, "aiko@tokyo-acme.jp")
    calls = await _cycle(monkeypatch, _cfg(), state)
    assert calls.index("replies") < calls.index("send")


@pytest.mark.asyncio
async def test_without_recipient_zones_nothing_is_sent_at_night(state, monkeypatch):
    pid = await seed_at(state, "Tokyo, Japan", "aiko@tokyo-acme.jp")
    await approved(state, pid, "aiko@tokyo-acme.jp")
    cfg = SellbirdConfig(**BASE, usage={"quiet_hours": {"background_work": True}},
                         channels={"email": {"provider": "gmail"}})
    calls = await _cycle(monkeypatch, cfg, state)
    assert "replies" not in calls and "send" not in calls


@pytest.mark.asyncio
async def test_off_by_default_quiet_hours_hold_background_work(state, monkeypatch):
    assert SellbirdConfig(**BASE).usage.quiet_hours.background_work is False
    calls = await _cycle(monkeypatch, _cfg(background=False), state)
    assert calls == []


@pytest.mark.parametrize("quiet_hours,expect", [
    ({"background_work": True}, "contacts no one keeps running"),
    ({}, "everything waits except cold email"),
    ({"enabled": False}, "off; Sellbird works around the clock"),
])
def test_status_says_what_quiet_hours_hold(tmp_path, monkeypatch, capsys, quiet_hours, expect):
    import argparse

    import sellbird.config as config_mod
    import sellbird.state as state_mod
    from sellbird import cli
    from sellbird.state import StateManager
    db = tmp_path / "s.db"
    asyncio.run(StateManager(str(db)).init_db())
    monkeypatch.setattr(state_mod, "StateManager", lambda *a, **k: StateManager(str(db)))
    cfg = SellbirdConfig(**BASE, usage={"quiet_hours": quiet_hours})
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    cli.cmd_status(argparse.Namespace(check=False))
    out = " ".join(capsys.readouterr().out.split())
    assert "Quiet hours:" in out and expect in out
