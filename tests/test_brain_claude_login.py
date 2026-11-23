"""An expired Claude login stops retries and shows as a paused brain until a
call works again, instead of failing quietly every cycle."""
import json

import pytest
import pytest_asyncio

import sellbird.brain as brain_mod
from sellbird.brain import BRAIN_STATUS_KEY, Brain
from sellbird.state import StateManager

LOGIN_ERROR = json.dumps({
    "type": "result", "is_error": True,
    "result": "Failed to authenticate: OAuth session expired and could not be refreshed"})
OK = json.dumps({"type": "result", "result": "ok", "usage": {"input_tokens": 1}})


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "s.db"))
    await s.init_db()
    return s


def _cli(monkeypatch, replies):
    """Each call answers with the next (returncode, stdout, stderr)."""
    calls = []

    class Proc:
        def __init__(self, reply):
            self.returncode, self._out, self._err = reply

        async def communicate(self, data=None):
            return self._out.encode(), self._err.encode()

    async def fake_exec(*cmd, **kw):
        calls.append(cmd)
        return Proc(replies[min(len(calls), len(replies)) - 1])

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0)
    return calls


async def _status(state):
    return json.loads(await state.get_setting(BRAIN_STATUS_KEY) or "{}")


@pytest.mark.asyncio
async def test_an_expired_login_on_stdout_is_not_retried_and_is_recorded(state, monkeypatch):
    calls = _cli(monkeypatch, [(1, LOGIN_ERROR, "")])
    assert await Brain(state).think("hi") == ""
    assert len(calls) == 1
    status = await _status(state)
    assert status["status"] == "paused" and status["kind"] == "auth"
    assert "/login" in status["reason"]


@pytest.mark.asyncio
async def test_a_login_error_inside_a_clean_exit_counts_too(state, monkeypatch):
    calls = _cli(monkeypatch, [(0, LOGIN_ERROR, "")])
    assert await Brain(state).think("hi") == ""
    assert len(calls) == 1
    assert (await _status(state))["kind"] == "auth"


@pytest.mark.asyncio
async def test_signing_in_again_clears_the_pause(state, monkeypatch):
    _cli(monkeypatch, [(1, LOGIN_ERROR, ""), (0, OK, "")])
    brain = Brain(state)
    assert await brain.think("hi") == ""
    assert await brain.think("hi") == "ok"
    assert (await _status(state))["status"] == "ok"


@pytest.mark.asyncio
async def test_a_pause_stored_by_an_earlier_run_clears_on_the_first_good_call(state, monkeypatch):
    await state.set_setting(BRAIN_STATUS_KEY, json.dumps(
        {"provider": "claude", "status": "paused", "kind": "auth", "reason": "x"}))
    _cli(monkeypatch, [(0, OK, "")])
    assert await Brain(state).think("hi") == "ok"
    assert (await _status(state))["status"] == "ok"


@pytest.mark.asyncio
async def test_other_failures_still_retry(state, monkeypatch):
    calls = _cli(monkeypatch, [(1, "", "overloaded"), (0, OK, "")])
    assert await Brain(state).think("hi") == "ok"
    assert len(calls) == 2
    assert (await _status(state)) == {}
