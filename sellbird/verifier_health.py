"""Whether the email verifiers are working, so a dead one can't hide.

An email verifier that rejects its key or runs out of credits looks, from the
outside, exactly like one that found nothing: every address stays a "guess"
and is never sent. This module keeps each verifier's last result in process
(the verifier calls note it), the heartbeat saves it to settings once a cycle,
and the dashboard and ``sellbird status`` read it from there.
"""

import json
import logging
import time

import httpx

logger = logging.getLogger("sellbird.verifier_health")

VERIFIER_STATUS_KEY = "verifier_status"
LABELS = {"reoon": "Reoon", "zerobounce": "ZeroBounce", "hunter": "Hunter"}

# After a failure, try that verifier again after this long: an outage (no
# credits until tomorrow, a key being renewed) ends without anyone telling
# Sellbird. Reoon is checked sooner through its free balance call.
RETRY_AFTER_SECONDS = 3600
# This many timeouts in a row count as a failure; one is just a slow server.
TIMEOUTS_BEFORE_FAILING = 3

# provider -> {"ok": bool, "reason": str, "at": epoch seconds, "timeouts": int}
_health: dict[str, dict] = {}
_saved: str = ""


def reset():
    """Forget everything noted (tests, a new process)."""
    global _saved
    _health.clear()
    _saved = ""


def note_ok(provider: str):
    _health[provider] = {"ok": True, "reason": "", "at": time.time(), "timeouts": 0}


def note_failure(provider: str, reason: str):
    previous = _health.get(provider) or {}
    if previous.get("ok", True) or previous.get("reason") != reason:
        logger.warning(f"Email verifier {LABELS.get(provider, provider)}: {reason}")
    _health[provider] = {"ok": False, "reason": reason, "at": time.time(),
                         "timeouts": previous.get("timeouts", 0)}


def note_timeout(provider: str):
    entry = dict(_health.get(provider) or {"ok": True, "reason": "", "at": time.time()})
    entry["timeouts"] = entry.get("timeouts", 0) + 1
    _health[provider] = entry
    if entry["timeouts"] >= TIMEOUTS_BEFORE_FAILING:
        note_failure(provider, f"timed out {entry['timeouts']} times in a row")


def note_response(provider: str, resp: httpx.Response) -> bool:
    """Note a verifier's HTTP answer. True if it can be read as a verdict."""
    code = resp.status_code
    if code == 200:
        return True
    if code in (401, 403):
        note_failure(provider, f"key rejected or out of credits (HTTP {code})")
    elif code == 402:
        note_failure(provider, "out of credits (HTTP 402)")
    elif code == 429:
        note_failure(provider, "rate limited (HTTP 429)")
    else:
        note_failure(provider, f"error from the service (HTTP {code})")
    return False


def failing(provider: str) -> str:
    """Why ``provider`` is failing right now, or "" if it isn't."""
    entry = _health.get(provider)
    return "" if not entry or entry.get("ok", True) else entry.get("reason", "")


def worth_trying(provider: str) -> bool:
    """Not failing, or failing long enough ago that the outage may be over."""
    entry = _health.get(provider)
    if not entry or entry.get("ok", True):
        return True
    return time.time() - entry.get("at", 0) >= RETRY_AFTER_SECONDS


def configured(env) -> list[str]:
    """The verifiers with a key, in the order Sellbird prefers them."""
    import os
    keys = {
        "reoon": getattr(env, "reoon_api_key", "") if env else "",
        "zerobounce": getattr(env, "zerobounce_api_key", "") if env else "",
        "hunter": (getattr(env, "hunter_api_key", "") if env else "")
        or os.getenv("HUNTER_API_KEY", ""),
    }
    return [name for name, key in keys.items() if (key or "").strip()]


def ready(env) -> bool:
    """Whether any configured verifier is worth a call now."""
    return any(worth_trying(p) for p in configured(env))


def snapshot(env) -> dict:
    """What the dashboard and ``sellbird status`` show."""
    providers = {}
    for name in configured(env):
        entry = _health.get(name) or {}
        providers[name] = {
            "label": LABELS.get(name, name),
            "status": ("failing" if not entry.get("ok", True)
                       else "ok" if entry else "unchecked"),
            "reason": entry.get("reason", ""),
            "since": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry["at"]))
                      if entry.get("at") else ""),
        }
    return {"configured": bool(providers), "providers": providers}


async def save(state, env):
    """Store the snapshot for other processes, only when it changed."""
    global _saved
    data = snapshot(env)
    raw = json.dumps(data, sort_keys=True)
    if raw == _saved:
        return
    try:
        await state.set_setting(VERIFIER_STATUS_KEY, raw)
        _saved = raw
    except Exception as e:
        logger.debug(f"Failed to save verifier status: {e}")


def parse(raw: str | None) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def problem(status: dict) -> str:
    """One line for the operator when no verifier works, else ""."""
    providers = (status or {}).get("providers") or {}
    if not providers:
        return ""
    bad = {n: p for n, p in providers.items() if p.get("status") == "failing"}
    if len(bad) < len(providers):
        return ""
    return "; ".join(f"{p.get('label', n)}: {p.get('reason', '')}" for n, p in bad.items())


def describe(saved: dict, env) -> tuple[str, str]:
    """(short state, detail) for ``sellbird status`` and the dashboard, from
    the saved snapshot and the keys configured now (a removed key is gone
    even if the last snapshot still lists it)."""
    names = configured(env)
    if not names:
        return ("NONE", "No email verifier key is set, so found emails stay guesses and "
                "are never sent. Add REOON_API_KEY, ZEROBOUNCE_API_KEY or HUNTER_API_KEY "
                "in .env.")
    providers = (saved or {}).get("providers") or {}
    parts, bad = [], []
    for name in names:
        p = providers.get(name) or {}
        label = LABELS.get(name, name)
        if p.get("status") == "failing":
            bad.append(name)
            parts.append(f"{label} failing since {p.get('since') or '?'}: {p.get('reason', '')}")
        elif p.get("status") == "ok":
            parts.append(f"{label} ok")
        else:
            parts.append(f"{label} not used yet")
    if len(bad) == len(names):
        return ("FAILING", "; ".join(parts) + ". Guessed emails are checked again once "
                "it works.")
    return ("ok", "; ".join(parts))
