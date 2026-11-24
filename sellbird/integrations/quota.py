"""Claude subscription quota gauge.

Reads the real 5-hour-window and weekly utilization the same way Claude
Code's own /usage command does: GET api.anthropic.com/api/oauth/usage with
the locally-stored OAuth token. The endpoint is undocumented, so every
failure degrades to None and callers fall back to Sellbird's own call
counting.

Anthropic allows only a few reads of this endpoint an hour per login, and
answers more with a 429 and a long Retry-After. So every Sellbird process
(the loop, the dashboard, `sellbird status`) shares one saved reading in the
data folder (``claude-quota.json``): the endpoint is asked at most once per
CACHE_TTL_OK across all of them, a 429's Retry-After holds for all of them,
and a good reading up to STALE_OK old is reused through an outage instead of
dropping straight to the call counter.
"""

import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from sellbird.usage import claude_config_dir

logger = logging.getLogger("sellbird.quota")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
CACHE_TTL_OK = 600.0     # seconds to reuse a good response, across processes
CACHE_TTL_FAIL = 300.0   # seconds to wait after a failure that isn't a 429
RATE_LIMIT_BACKOFF = 900.0   # first wait after a 429 with no Retry-After
MAX_BACKOFF = 3600.0         # longest self-chosen wait while rate limited
STALE_OK = 3600.0            # reuse a good reading this long through an outage
SHARED_FILE = "claude-quota.json"
REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

# macOS keychain item Claude Code stores credentials under.
KEYCHAIN_SERVICE = "Claude Code-credentials"


def _token_from_json_blob(blob: str) -> Optional[str]:
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    if isinstance(oauth, dict):
        token = oauth.get("accessToken")
        if isinstance(token, str) and token:
            return token
    return None


def get_oauth_token() -> Optional[str]:
    """Find the Claude Code OAuth access token on this machine.

    Tries the credentials file (Linux / CLAUDE_CONFIG_DIR overrides),
    then the macOS keychain. Returns None when not logged in.
    """
    creds_file = claude_config_dir() / ".credentials.json"
    try:
        if creds_file.is_file():
            token = _token_from_json_blob(creds_file.read_text(encoding="utf-8"))
            if token:
                return token
    except OSError as e:
        logger.debug(f"Could not read {creds_file}: {e}")

    # Default config dir fallback, in case CLAUDE_CONFIG_DIR points elsewhere
    default_creds = Path.home() / ".claude" / ".credentials.json"
    if default_creds != creds_file:
        try:
            if default_creds.is_file():
                token = _token_from_json_blob(default_creds.read_text(encoding="utf-8"))
                if token:
                    return token
        except OSError:
            pass

    if sys.platform == "darwin":
        # Claude Code stores credentials per config dir: the default item is
        # "Claude Code-credentials", but a CLAUDE_CONFIG_DIR install uses a
        # hash-suffixed service name (and the default item may hold an empty
        # token). Enumerate every matching service and take the first item
        # with a real token.
        for service in _keychain_services():
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", service, "-w"],
                    capture_output=True, text=True, timeout=5,
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.debug(f"Keychain lookup failed for {service}: {e}")
                continue
            if result.returncode == 0:
                token = _token_from_json_blob(result.stdout.strip())
                if token:
                    return token

    return None


def _keychain_services() -> list[str]:
    """All keychain service names Claude Code may store credentials under."""
    services = [KEYCHAIN_SERVICE]
    try:
        dump = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True, text=True, timeout=10,
        )
        if dump.returncode == 0:
            for line in dump.stdout.splitlines():
                # Lines look like:  "svce"<blob>="Claude Code-credentials-53f815ad"
                if '"svce"<blob>="' not in line:
                    continue
                name = line.split('"svce"<blob>="', 1)[1].rstrip('"')
                if name.startswith(KEYCHAIN_SERVICE) and name not in services:
                    services.append(name)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"Keychain enumeration failed: {e}")
    return services


def _parse_window(value) -> Optional[dict]:
    """Normalize one utilization window to {'utilization': float, 'resets_at': str}."""
    if not isinstance(value, dict):
        return None
    utilization = value.get("utilization")
    if not isinstance(utilization, (int, float)):
        return None
    resets_at = value.get("resets_at") or value.get("resetsAt") or ""
    return {
        "utilization": float(utilization),
        "resets_at": str(resets_at) if resets_at else "",
    }


def _retry_after(resp) -> Optional[float]:
    """Seconds from a Retry-After header, if it holds a number."""
    try:
        value = float(resp.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def shared_path() -> Optional[Path]:
    """Where every Sellbird process keeps the last reading, or None."""
    try:
        from sellbird.workspace_home import current_home
        return Path(current_home().data_dir) / SHARED_FILE
    except Exception:
        return None


def _load_shared() -> dict:
    path = shared_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_shared(data: dict) -> None:
    path = shared_path()
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.debug(f"Could not save the quota reading: {e}")


class QuotaClient:
    """Reader of Claude subscription utilization, sharing one saved reading
    with every other Sellbird process (see the module docstring)."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._shared: dict = {}
        self.problem = ""  # why the last read failed ("" when it worked)

    async def get_utilization(self) -> Optional[dict]:
        """Return {'five_hour': {...}, 'seven_day': {...}} or None.

        Each window dict has 'utilization' (percent, 0-100) and
        'resets_at' (ISO timestamp or ''). Missing windows are omitted;
        an empty result is reported as None. While the endpoint can't be
        read, a good reading up to STALE_OK old is returned instead of None.
        """
        async with self._lock:
            now = time.time()
            shared = _load_shared()
            read_at = float(shared.get("read_at") or 0)
            next_at = float(shared.get("next_read_at") or 0)
            if now >= next_at and not (shared.get("windows") and now - read_at < CACHE_TTL_OK):
                result, problem, wait = await self._read(float(shared.get("backoff") or 0))
                shared = _load_shared()  # another process may have read meanwhile
                if result:
                    shared.update(windows=result, read_at=now, problem="", backoff=0,
                                  next_read_at=now + CACHE_TTL_OK)
                else:
                    shared.update(problem=problem,
                                  backoff=wait if wait is not None else shared.get("backoff", 0),
                                  next_read_at=now + (wait if wait is not None else CACHE_TTL_FAIL))
                _save_shared(shared)
            self._shared = shared
            self.problem = shared.get("problem", "")
            windows = shared.get("windows")
            age = now - float(shared.get("read_at") or 0)
            if windows and age < STALE_OK:
                return windows
            return None

    def status(self) -> dict:
        """Where the last answer came from: live (read within CACHE_TTL_OK),
        saved (an older good reading reused through an outage) or
        unreadable, and why."""
        shared = self._shared
        now = time.time()
        windows = shared.get("windows")
        age_s = now - float(shared.get("read_at") or 0)
        age = int(age_s // 60) if windows else None
        if windows and age_s < CACHE_TTL_OK and not shared.get("problem"):
            return {"source": "live", "problem": "", "age_minutes": age}
        if windows and age_s < STALE_OK:
            return {"source": "saved", "problem": shared.get("problem", ""), "age_minutes": age}
        return {"source": "unreadable", "problem": shared.get("problem") or "not read yet",
                "age_minutes": age}

    async def _read(self, backoff: float = 0.0) -> tuple[Optional[dict], str, Optional[float]]:
        """One read: (windows or None, problem, seconds to wait if rate limited)."""
        token = await asyncio.get_event_loop().run_in_executor(None, get_oauth_token)
        if not token:
            logger.debug("Quota: no Claude OAuth token found; falling back.")
            return None, "no Claude login found (run 'claude' and /login)", None

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.get(
                    USAGE_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "anthropic-beta": OAUTH_BETA_HEADER,
                    },
                )
        except httpx.HTTPError as e:
            logger.debug(f"Quota fetch failed: {e}")
            return None, f"network error ({type(e).__name__})", None

        if resp.status_code == 429:
            # Anthropic's Retry-After is obeyed as given (it can be over half
            # an hour); without one, back off 15, 30, then 60 minutes.
            wait = _retry_after(resp)
            if wait is None:
                wait = min(max(backoff * 2, RATE_LIMIT_BACKOFF), MAX_BACKOFF)
            logger.info(f"Claude usage endpoint is rate limiting reads; next read in "
                        f"{wait / 60:.0f} min.")
            return None, "Anthropic's usage endpoint is rate limiting reads (HTTP 429)", wait
        if resp.status_code != 200:
            logger.debug(f"Quota endpoint returned {resp.status_code}.")
            return None, f"usage endpoint answered HTTP {resp.status_code}", None

        try:
            payload = resp.json()
        except ValueError:
            return None, "usage endpoint sent an unreadable answer", None
        if not isinstance(payload, dict):
            return None, "usage endpoint sent an unreadable answer", None

        windows = {}
        for key in ("five_hour", "seven_day"):
            parsed = _parse_window(payload.get(key))
            if parsed:
                windows[key] = parsed
        if not windows:
            return None, "usage endpoint sent no usage windows", None
        return windows, "", None
