"""A rate-limited quota endpoint backs off, reuses a recent reading, and a
pause from Sellbird's own call count says so instead of looking like the
plan ran out."""
import json

import httpx
import pytest
import pytest_asyncio

import sellbird.integrations.quota as quota_mod
from sellbird.brain import QUOTA_STATUS_KEY, Brain, describe_quota
from sellbird.config import UsageConfig
from sellbird.state import StateManager

GOOD = {"five_hour": {"utilization": 20.0, "resets_at": "soon"},
        "seven_day": {"utilization": 10.0, "resets_at": "later"}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Endpoint:
    """Stands in for httpx.AsyncClient; answers from a list of responses."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def client(self, *a, **k):
        endpoint = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                endpoint.calls += 1
                status, body, hdrs = endpoint.answers.pop(0)
                return httpx.Response(status, json=body, headers=hdrs,
                                      request=httpx.Request("GET", url))
        return Client()


@pytest.fixture
def clock(monkeypatch, tmp_path):
    c = Clock()
    monkeypatch.setattr(quota_mod.time, "time", c)
    monkeypatch.setattr(quota_mod, "get_oauth_token", lambda: "t")
    monkeypatch.setattr(quota_mod, "shared_path", lambda: tmp_path / "claude-quota.json")
    return c


def _endpoint(monkeypatch, answers):
    e = Endpoint(answers)
    monkeypatch.setattr(quota_mod.httpx, "AsyncClient", e.client)
    return e


@pytest.mark.asyncio
async def test_429_honours_retry_after_and_does_not_hammer(monkeypatch, clock):
    e = _endpoint(monkeypatch, [(429, {}, {"retry-after": "120"}), (200, GOOD, {})])
    q = quota_mod.QuotaClient()
    assert await q.get_utilization() is None
    assert "429" in q.status()["problem"]
    clock.now += 60
    assert await q.get_utilization() is None
    assert e.calls == 1  # still inside Retry-After: no second request
    clock.now += 61
    assert await q.get_utilization() == GOOD
    assert q.status() == {"source": "live", "problem": "", "age_minutes": 0}


@pytest.mark.asyncio
async def test_429_without_retry_after_backs_off_longer_each_time(monkeypatch, clock):
    _endpoint(monkeypatch, [(429, {}, {})] * 3)
    q = quota_mod.QuotaClient()
    waits = []
    for _ in range(3):
        await q.get_utilization()
        wait = quota_mod._load_shared()["next_read_at"] - clock.now
        waits.append(wait)
        clock.now += wait + 1
    assert waits == [quota_mod.RATE_LIMIT_BACKOFF, quota_mod.RATE_LIMIT_BACKOFF * 2,
                     quota_mod.MAX_BACKOFF]


@pytest.mark.asyncio
async def test_recent_reading_is_reused_through_an_outage(monkeypatch, clock):
    _endpoint(monkeypatch, [(200, GOOD, {}), (429, {}, {}), (429, {}, {})])
    q = quota_mod.QuotaClient()
    assert await q.get_utilization() == GOOD
    clock.now += quota_mod.CACHE_TTL_OK + 1
    assert await q.get_utilization() == GOOD  # 429 now, but the reading is recent
    assert q.status()["source"] == "saved"
    clock.now += quota_mod.STALE_OK
    assert await q.get_utilization() is None  # too old to trust
    assert q.status()["source"] == "unreadable"


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "q.db"))
    await s.init_db()
    return s


class NoQuota:
    async def get_utilization(self):
        return None

    def status(self):
        return {"source": "unreadable", "age_minutes": None,
                "problem": "Anthropic's usage endpoint is rate limiting reads (HTTP 429)"}


@pytest.mark.asyncio
async def test_call_count_pause_is_recorded_with_the_reason(state):
    brain = Brain(state)
    brain.quota = NoQuota()
    for _ in range(3):
        await state.increment_usage(provider="claude")
    assert await brain.is_within_budget(3, max_percent=60) is False
    raw = await state.get_setting(QUOTA_STATUS_KEY)
    saved = json.loads(raw)
    assert saved["source"] == "calls" and saved["calls"] == 3 and saved["within"] is False
    line = describe_quota(raw, "claude")
    assert "counting calls" in line and "3 of 3" in line and "429" in line
    assert describe_quota(raw, "codex") == ""  # another provider's record says nothing


@pytest.mark.asyncio
async def test_live_quota_is_recorded(state):
    class Live:
        async def get_utilization(self):
            return GOOD

        def status(self):
            return {"source": "live", "problem": "", "age_minutes": 0}

    brain = Brain(state)
    brain.quota = Live()
    assert await brain.is_within_budget(1, max_percent=60) is True
    line = describe_quota(await state.get_setting(QUOTA_STATUS_KEY), "claude")
    assert line == "20% of plan used (limit 60%)"


def test_fallback_daily_calls_is_configurable():
    assert UsageConfig().fallback_daily_calls == 200
    assert UsageConfig(fallback_daily_calls=600).fallback_daily_calls == 600
    with pytest.raises(ValueError):
        UsageConfig(fallback_daily_calls=0)


# ── the Usage page says what the budget went by ──

from tests.test_dashboard_signals import client  # noqa: E402,F401  (fixture)


def test_usage_page_shows_the_reason_and_the_budget(client, monkeypatch):
    import asyncio as aio

    import sellbird.dashboard as dash

    class Limited:
        async def get_utilization(self):
            return None

        def status(self):
            return NoQuota().status()
    monkeypatch.setattr(dash, "_quota_client_for", lambda provider, model="": Limited())
    monkeypatch.setattr(dash, "_active_brain", lambda: type("B", (), {"provider": "claude",
                                                                      "model": ""})())
    st = StateManager(str(client.db_path))
    aio.run(st.init_db())
    aio.run(st.set_setting(QUOTA_STATUS_KEY, json.dumps({
        "provider": "claude", "source": "calls", "calls": 122, "max_calls": 120,
        "problem": "Anthropic's usage endpoint is rate limiting reads (HTTP 429)"})))
    data = client.get("/api/usage").json()
    assert "429" in data["quota_problem"]
    assert "122 of 120" in data["budget_note"]


@pytest.mark.asyncio
async def test_every_process_shares_one_reading(monkeypatch, clock):
    """The loop, the dashboard and `sellbird status` each make a client; only
    one asks Anthropic, and a 429 holds for all of them."""
    e = _endpoint(monkeypatch, [(200, GOOD, {}), (429, {}, {"retry-after": "2000"}), (200, GOOD, {})])
    loop, dashboard, status = (quota_mod.QuotaClient() for _ in range(3))
    assert await loop.get_utilization() == GOOD
    assert await dashboard.get_utilization() == GOOD
    assert await status.get_utilization() == GOOD
    assert e.calls == 1
    clock.now += quota_mod.CACHE_TTL_OK + 1
    await loop.get_utilization()                # 429, Retry-After 2000 s
    clock.now += 600
    assert await dashboard.get_utilization() == GOOD   # the saved reading, no request
    assert e.calls == 2
    assert dashboard.status()["source"] == "saved"
    clock.now += 1500
    assert await status.get_utilization() == GOOD
    assert e.calls == 3 and status.status()["source"] == "live"
