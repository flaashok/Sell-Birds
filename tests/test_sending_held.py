"""When Sellbird holds sending because replies couldn't be read, you see it.

Sending waits for replies so an opt-out stops its follow-up (main.py). That
used to show only as a log line, so sending could stall for days unnoticed:
a Gmail sign-in that lapsed, an inbox over the read limit. Now the hold is
recorded with why and since when, and Today and `sellbird status` say so.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from sellbird.sending_mode import held_message
from sellbird.state import SENDS_HELD_KEY, StateManager
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "h.db"))
    await s.init_db()
    return s


# ── the handler says why it couldn't read ──


@pytest.mark.asyncio
async def test_the_handler_keeps_the_reason_a_read_failed(state):
    from tests.test_outbox_native import FakeProvider, make_handler
    provider = FakeProvider()
    provider.get_replies = AsyncMock(side_effect=RuntimeError("not signed in to Gmail"))
    handler = make_handler(state, provider)
    assert await handler._run_native() is False
    assert "not signed in to Gmail" in handler.last_read_problem
    provider.get_replies = AsyncMock(return_value=[])
    assert await handler._run_native() is True
    assert handler.last_read_problem == ""


@pytest.mark.asyncio
async def test_gmail_without_a_sign_in_says_so():
    from sellbird.integrations.gmail import GmailError, GmailProvider
    p = GmailProvider(SimpleNamespace(), SimpleNamespace())
    p._token = AsyncMock(return_value=None)
    with pytest.raises(GmailError, match="sellbird gmail auth"):
        await p.get_replies()


# ── the hold is recorded, kept and cleared ──


@pytest.mark.asyncio
async def test_a_hold_keeps_its_start_and_clears_when_sending_runs(state):
    await state.note_sends_held("Gmail: not signed in")
    first = await state.current_sends_held()
    await state.note_sends_held("Gmail: not signed in")
    again = await state.current_sends_held()
    assert again["since"] == first["since"] and again["reason"] == "Gmail: not signed in"
    await state.clear_sends_held()
    assert await state.current_sends_held() is None


@pytest.mark.asyncio
async def test_a_hold_nobody_has_seen_for_half_a_day_is_not_shown(state):
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).replace(tzinfo=None).isoformat()
    await state.set_setting(SENDS_HELD_KEY, json.dumps({"since": old, "last_seen": old, "reason": "x"}))
    assert await state.current_sends_held() is None        # Sellbird stopped: no stale card


@pytest.mark.asyncio
async def test_the_heartbeat_records_and_clears_it(state):
    from sellbird.main import record_send_phase
    await record_send_phase(state, "replies couldn't all be read: Gmail: not signed in")
    assert "not signed in" in (await state.current_sends_held())["reason"]
    await record_send_phase(state, "")
    assert await state.current_sends_held() is None


# ── the wording ──


@pytest.mark.parametrize("reason,fix", [
    ("Gmail: not signed in to Gmail. Run 'sellbird gmail auth'.", "sellbird gmail auth"),
    ("Microsoft: not authorized. Run 'sellbird microsoft auth'.", "sellbird microsoft auth"),
    ("inbox has more than 40 pages; refusing a partial read", "more mail"),
    ("Graph 503 on GET /me/messages", "tries again every cycle"),
])
def test_the_message_names_the_fix(reason, fix):
    title, detail = held_message({"reason": reason, "since": "2026-10-24T08:00:00"}, waiting=3)
    assert "isn't sending" in title
    assert fix in detail and "3 emails are waiting" in detail and "opt-out" in detail


# ── where it shows ──


def test_today_shows_the_hold(client):
    st = StateManager(str(client.db_path))
    asyncio.run(st.init_db())
    asyncio.run(st.note_sends_held("Gmail: not signed in to Gmail. Run 'sellbird gmail auth'."))
    items = client.get("/api/today").json()["items"]
    held = [i for i in items if i["key"] == "sends-held"]
    assert held and held[0]["tone"] == "bad" and "sellbird gmail auth" in held[0]["detail"]
    asyncio.run(st.clear_sends_held())
    assert not [i for i in client.get("/api/today").json()["items"] if i["key"] == "sends-held"]


def test_sellbird_status_shows_it(tmp_path, monkeypatch, capsys):
    import argparse
    import sellbird.state as state_mod
    from sellbird import cli
    db = tmp_path / "s.db"
    st = StateManager(str(db))
    asyncio.run(st.init_db())
    asyncio.run(st.note_sends_held("Gmail: not signed in to Gmail. Run 'sellbird gmail auth'."))
    real = state_mod.StateManager
    monkeypatch.setattr(state_mod, "StateManager", lambda *a, **k: real(str(db)))
    cli.cmd_status(argparse.Namespace(check=False))
    out = capsys.readouterr().out
    assert "NOT SENDING" in out and "sellbird gmail auth" in out


# ── review of the notice ──


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,status,body,expect", [
    (None, None, None, "sellbird gmail auth"),                        # never signed in
    ({"refresh_token": "r"}, 400, {"error": "invalid_grant"}, "sellbird gmail auth"),
    ({"refresh_token": "r"}, "network", None, "couldn't reach Google"),
    ({"refresh_token": "r"}, 503, {}, "couldn't reach Google"),
])
async def test_gmail_says_which_sign_in_problem_it_is(monkeypatch, stored, status, body, expect):
    import httpx
    import sellbird.integrations.gmail as gmail
    def respond(request):
        if status == "network":
            raise httpx.ConnectError("down")
        return httpx.Response(status, json=body)
    client_cls = httpx.AsyncClient
    monkeypatch.setattr(gmail.httpx, "AsyncClient",
                        lambda **kw: client_cls(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(gmail, "_load_token", lambda: stored)
    p = gmail.GmailProvider(SimpleNamespace(), SimpleNamespace(gmail_client_id="i", gmail_client_secret="s"))
    with pytest.raises(gmail.GmailError) as e:
        await p.get_replies()
    assert expect in str(e.value)
    if expect != "sellbird gmail auth":
        assert "sellbird gmail auth" not in str(e.value)


def test_a_reply_that_couldnt_be_handled_is_worded_as_that():
    reason = "a reply from ann@gmail.com couldn't be handled (KeyError: 'signature' not authorized pages)"
    title, detail = held_message({"reason": reason, "since": "2026-10-24T08:00:00"}, waiting=1)
    assert "couldn't handle a reply" in title
    assert "sellbird gmail auth" not in detail and "more mail" not in detail
    assert "1 email is waiting" in detail


@pytest.mark.asyncio
async def test_a_new_hold_after_a_stale_one_starts_now(state):
    old = (datetime.now(timezone.utc) - timedelta(days=3)).replace(tzinfo=None).isoformat()
    await state.set_setting(SENDS_HELD_KEY, json.dumps({"since": old, "last_seen": old, "reason": "x"}))
    await state.note_sends_held("Gmail: not signed in")
    assert (await state.current_sends_held())["since"] > old


@pytest.mark.parametrize("unread,paused,problem,expected", [
    (["read_replies"], "", "gmail: not signed in", "gmail: not signed in"),
    (["read_replies"], "", "", "replies couldn't all be read and handled."),
    (["read_replies"], "Codex is paused", "gmail: x", None),      # the brain card covers it
    ([], "", "", ""),                                             # sending ran: clear
])
def test_when_the_heartbeat_records_a_hold(unread, paused, problem, expected):
    from sellbird.main import send_hold_reason
    assert send_hold_reason(unread, paused, problem) == expected


def test_the_outbox_card_on_today_says_which_mode(client, monkeypatch):
    import sellbird.dashboard as dash
    from tests.test_outbox_native import _now_iso
    st = StateManager(str(client.db_path))
    asyncio.run(st.init_db())
    import sqlite3
    db = sqlite3.connect(client.db_path)
    with db:   # commits; close() below lets Windows delete the temp database
        db.execute("INSERT INTO outbox (id, prospect_id, kind, step, to_email, subject, body, status, send_at) "
                   "VALUES ('o1', 'p', 'reply', 1, 'a@x.test', 's', 'b', 'pending_review', ?)", (_now_iso(),))
    db.close()
    for approval, words in ((True, "Nothing sends until you approve it"), (False, "Autopilot sends the rest")):
        monkeypatch.setattr(dash, "_require_approval_now", lambda a=approval: a)
        [card] = [i for i in client.get("/api/today").json()["items"] if i["key"] == "outbox"]
        assert words in card["detail"]


# ── an IMAP inbox that can't log in holds sending instead of reading as empty ──


def _imap(monkeypatch, error):
    from sellbird.integrations import smtp_mail

    class Conn:
        def __init__(self, *a, **k):
            pass

        def login(self, user, password):
            raise error

        def logout(self):
            pass

    monkeypatch.setattr(smtp_mail.imaplib, "IMAP4_SSL", Conn)
    settings = smtp_mail.SmtpSettings(host="smtp.example.org", username="sales@example.org",
                                      password="pw", imap_host="imap.example.org")
    from sellbird.integrations.mail_provider import MailIdentity
    return smtp_mail.SmtpImapProvider(SimpleNamespace(), SimpleNamespace(),
                                      identity=MailIdentity("sales@example.org", "Sam"),
                                      smtp=settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("server_says,expect", [
    (b"[ALERT] Application-specific password required: https://support.example.org (Failure)",
     "needs an app password"),
    (b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)", "invalid credentials"),
    (b"something else broke", "something else broke"),
])
async def test_an_imap_login_failure_is_raised_with_the_fix(monkeypatch, server_says, expect):
    from sellbird.integrations.smtp_mail import ImapReadError
    import imaplib
    provider = _imap(monkeypatch, imaplib.IMAP4.error(server_says))
    with pytest.raises(ImapReadError, match=expect) as info:
        await provider.get_replies()
    assert "sales@example.org" in str(info.value) or expect == "something else broke"


@pytest.mark.asyncio
async def test_every_failing_inbox_is_named(state):
    from tests.test_outbox_native import FakeProvider, make_handler
    handler = make_handler(state, FakeProvider())
    for name in ("one", "two"):
        bad = FakeProvider()
        bad.get_replies = AsyncMock(side_effect=RuntimeError(f"{name} refused"))
        assert await handler._read_inbox(bad, None, label=f"{name}@example.org") is False
    assert "one refused" in handler.last_read_problem
    assert "two refused" in handler.last_read_problem


def test_an_imap_login_hold_names_the_fix():
    reason = ("sales@example.org (smtp): IMAP login for sales@example.org needs an app "
              "password: the mail provider refused the account password.")
    title, detail = held_message({"reason": reason, "since": "2026-10-24T08:00:00"}, waiting=2)
    assert "couldn't read your inbox" in title
    assert "sellbird inboxes connect" in detail and "2 emails are waiting" in detail


# ── a held send phase doesn't win the cycle ──

@pytest.mark.asyncio
@pytest.mark.parametrize("held, action", [(True, "prospect"), (False, "send_campaign")])
async def test_drafts_held_by_an_unread_inbox_dont_starve_prospecting(held, action):
    from sellbird.main import decide_next_action
    email = SimpleNamespace(enabled=True, provider="smtp", require_approval=True)
    config = SimpleNamespace(channels=SimpleNamespace(email=email))
    summary = {"draft_campaigns": 4, "new_prospects": 0, "writable_prospects": 0,
               "sends_held": held}
    assert await decide_next_action(None, None, config, summary=summary) == action


@pytest.mark.asyncio
async def test_the_summary_says_when_sending_is_held(state):
    assert (await state.get_state_summary())["sends_held"] is False
    await state.note_sends_held("IMAP login was refused (invalid credentials).")
    assert (await state.get_state_summary())["sends_held"] is True
