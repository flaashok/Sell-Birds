"""A dead email verifier is shown instead of hidden, and guessed emails are
checked again once a verifier works."""
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from sellbird import recheck, verifier_health
from sellbird.integrations import email_finder
from sellbird.main import unusable_backlog_notice
from sellbird.models import Prospect
from sellbird.state import StateManager

REOON = SimpleNamespace(reoon_api_key="k", zerobounce_api_key="", hunter_api_key="")
NONE = SimpleNamespace(reoon_api_key="", zerobounce_api_key="", hunter_api_key="")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    verifier_health.reset()
    yield
    verifier_health.reset()


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "v.db"))
    await s.init_db()
    return s


def _reoon(monkeypatch, handler):
    """Route Reoon calls to ``handler(request) -> httpx.Response``."""
    real = httpx.AsyncClient

    def client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    monkeypatch.setattr(email_finder.httpx, "AsyncClient", client)


# ── Health ──

@pytest.mark.asyncio
async def test_a_rejected_key_is_recorded_as_failing(monkeypatch):
    _reoon(monkeypatch, lambda r: httpx.Response(403))
    assert await email_finder.verify_reoon("a@shop.example", "k") is None
    assert "403" in verifier_health.failing("reoon")
    assert not verifier_health.ready(REOON)


@pytest.mark.asyncio
async def test_a_good_answer_clears_a_failure(monkeypatch):
    verifier_health.note_failure("reoon", "out of credits")
    _reoon(monkeypatch, lambda r: httpx.Response(200, json={"status": "safe"}))
    assert (await email_finder.verify_reoon("a@shop.example", "k"))["status"] == "safe"
    assert verifier_health.failing("reoon") == ""


def test_one_timeout_is_a_slow_server_three_are_a_failure():
    verifier_health.note_timeout("reoon")
    assert verifier_health.failing("reoon") == ""
    verifier_health.note_timeout("reoon")
    verifier_health.note_timeout("reoon")
    assert "timed out" in verifier_health.failing("reoon")


def test_a_failure_is_retried_after_a_while(monkeypatch):
    verifier_health.note_failure("reoon", "no credits")
    assert not verifier_health.ready(REOON)
    monkeypatch.setattr(verifier_health, "RETRY_AFTER_SECONDS", 0)
    assert verifier_health.ready(REOON)


@pytest.mark.asyncio
async def test_reoon_balance_says_when_credits_are_gone_and_back(monkeypatch):
    left = {"n": 0}
    _reoon(monkeypatch, lambda r: httpx.Response(200, json={
        "status": "success", "remaining_daily_credits": left["n"],
        "remaining_instant_credits": 0}))
    assert await email_finder.reoon_credits("k") == 0
    assert "credits" in verifier_health.failing("reoon")
    left["n"] = 20
    assert await email_finder.reoon_credits("k") == 20
    assert verifier_health.failing("reoon") == ""


def test_describe_names_each_state():
    assert verifier_health.describe({}, NONE)[0] == "NONE"
    assert verifier_health.describe({}, REOON) == ("ok", "Reoon not used yet")
    verifier_health.note_failure("reoon", "key rejected or out of credits (HTTP 403)")
    saved = verifier_health.snapshot(REOON)
    state, detail = verifier_health.describe(saved, REOON)
    assert state == "FAILING" and "HTTP 403" in detail


@pytest.mark.asyncio
async def test_the_snapshot_is_saved_for_the_dashboard(state):
    verifier_health.note_failure("reoon", "out of credits")
    await verifier_health.save(state, REOON)
    saved = json.loads(await state.get_setting(verifier_health.VERIFIER_STATUS_KEY))
    assert saved["providers"]["reoon"]["status"] == "failing"
    assert verifier_health.problem(saved) == "Reoon: out of credits"


def test_the_pause_notice_names_the_broken_verifier():
    summary = {"prospects": {"new": 25}, "writable_prospects": 0,
               "recent_unusable_prospects": 25, "verifier_problem": "Reoon: out of credits"}
    notice = unusable_backlog_notice(summary)
    assert "Reoon: out of credits" in notice and "checked again" in notice


# ── Re-checking guessed emails ──

async def _guess(state, email, status="guess", score=0):
    return await state.add_prospect(Prospect(
        first_name="A", last_name=email.split("@")[0], email=email, email_status=status,
        company="Shop", title="Owner", score=score))


async def _status(state, pid):
    return (await state.get_prospect(pid)).email_status


def _grades(monkeypatch, grades):
    seen = []

    async def grade(email, env=None):
        seen.append(email)
        value = grades.get(email, "guess")
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value
    monkeypatch.setattr(recheck, "verify_published_address", grade)

    async def credits(key):
        return 100
    monkeypatch.setattr(recheck, "reoon_credits", credits)
    return seen


@pytest.mark.asyncio
async def test_guesses_move_once_a_verifier_works(state, monkeypatch):
    good = await _guess(state, "good@a.example")
    bad = await _guess(state, "bad@b.example")
    unclear = await _guess(state, "unclear@c.example")
    done = await _guess(state, "done@d.example", status="verified")
    seen = _grades(monkeypatch, {"good@a.example": "verified", "bad@b.example": "invalid"})
    assert await state.count_recheckable_emails() == 3

    counts = await recheck.recheck_guessed_emails(state, REOON)

    assert counts == {"verified": 1, "invalid": 1, "guess": 1}
    assert "done@d.example" not in seen
    assert await _status(state, good) == "verified"
    assert await _status(state, bad) == "invalid"
    assert await _status(state, unclear) == "guess"
    assert await _status(state, done) == "verified"
    # The unclear one waits before another try instead of spending a credit each cycle.
    assert await state.count_recheckable_emails() == 0
    assert await state.count_writable_prospects() == 2


@pytest.mark.asyncio
async def test_nothing_is_checked_without_a_working_verifier(state, monkeypatch):
    await _guess(state, "x@a.example")
    seen = _grades(monkeypatch, {})
    assert await recheck.recheck_guessed_emails(state, NONE) == {}
    verifier_health.note_failure("reoon", "no credits")
    assert await recheck.recheck_guessed_emails(state, REOON) == {}
    assert seen == []


@pytest.mark.asyncio
async def test_a_verifier_breaking_mid_batch_leaves_the_rest_unchecked(state, monkeypatch):
    first = await _guess(state, "first@a.example", score=2)
    second = await _guess(state, "second@b.example", score=1)

    def breaks():
        verifier_health.note_failure("reoon", "out of credits")
        return "guess"
    seen = _grades(monkeypatch, {"first@a.example": breaks})
    await recheck.recheck_guessed_emails(state, REOON)
    assert seen == ["first@a.example"]
    # Not stamped: it gets a real check once the verifier is back.
    assert await state.count_recheckable_emails() == 2
    assert await _status(state, first) == "guess" and await _status(state, second) == "guess"


@pytest.mark.asyncio
async def test_reoon_credits_cap_the_batch(state, monkeypatch):
    for i in range(5):
        await _guess(state, f"p{i}@a{i}.example")
    seen = _grades(monkeypatch, {})

    async def two(key):
        return 2
    monkeypatch.setattr(recheck, "reoon_credits", two)
    await recheck.recheck_guessed_emails(state, REOON)
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_the_summary_counts_what_can_be_rechecked(state):
    await _guess(state, "x@a.example")
    summary = await state.get_state_summary()
    assert summary["recheckable_emails"] == 1
