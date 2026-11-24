"""Automatic discovery: the loop keeps the company list topped up.

Discovery used to run only when someone pressed Run on the Discover page,
and only inside the dashboard process. With ``discovery.auto: true`` the
heartbeat starts a small run on its own whenever the list of companies the
Scout hasn't checked yet drops below about ``backlog_days`` of its work.

Each run takes the next ``queries_per_run`` searches from the same list the
Discover page would build, so every search is made once before any repeats;
the place in that list is saved, so a restart carries on where it stopped.
When every search (and every configured preset) has run, discovery waits
``repeat_after_days`` before starting the list again.

Spending is opt-in: with ``max_daily_spend: 0`` (the default) only a source
whose estimate is $0 runs. The Discover page's "Automatic discovery" card has
Pause and Resume (``discovery_auto_paused``); pausing also stops a run that is
going, within a few seconds. That is separate from the page's own Run and
Stop, which start and stop one run by hand: while one of those is going,
automatic discovery waits. What it is doing, and why when it can't, is saved
under ``discovery_auto`` for `sellbird status`, the dashboard header and the
Discover page.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("sellbird.auto_discovery")

AUTO_KEY = "discovery_auto"
AUTO_PAUSED_KEY = "discovery_auto_paused"   # set by Pause on the Discover page
PAGE_RUN_KEY = "discover_page_run"   # {"pid", "kind", "started"} while the page's run goes
RETRY_AFTER_PROBLEM = 3600       # a source that can't run is tried again hourly
WATCH_SECONDS = 5                # how often a running top-up checks for Pause
PAUSED_REASON = "paused; press Resume on the Discover page to start it again"


def _now() -> float:
    return time.time()


def _stamp(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or _now(), timezone.utc).isoformat(timespec="seconds")


def parse(raw: str | None) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def plan_signature(queries) -> str:
    """Identifies a search list, so a changed ICP starts from the top."""
    text = "\n".join(f"{q.keyword()}|{getattr(q, 'location', '')}" for q in queries)
    return hashlib.sha1(text.encode()).hexdigest()[:12]


async def unchecked_companies(state) -> int:
    """Companies with a website the Scout has never looked at for people."""
    async with state._connect() as db:
        async with db.execute(
            """SELECT COUNT(*) FROM companies c
               WHERE c.domain != '' AND c.prospects_checked_at IS NULL
                 AND NOT EXISTS (SELECT 1 FROM prospects p WHERE p.company_id = c.id)"""
        ) as cur:
            return int((await cur.fetchone())[0] or 0)


async def checked_last_day(state) -> int:
    """How many companies the Scout got through in the last 24 hours."""
    async with state._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM companies WHERE prospects_checked_at >= datetime('now', '-1 day')"
        ) as cur:
            return int((await cur.fetchone())[0] or 0)


async def spent_today(state) -> float:
    async with state._connect() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE stage = 'discover' "
            "AND started_at >= datetime('now', 'start of day')"
        ) as cur:
            return float((await cur.fetchone())[0] or 0.0)


def _alive(pid) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def auto_run_going(raw: str | None) -> bool:
    """An automatic top-up is running now (its loop process is alive)."""
    saved = parse(raw)
    return saved.get("state") == "running" and _alive(saved.get("run_pid"))


def page_run_going(raw: str | None) -> bool:
    """A run started from the Discover page is still going: the dashboard
    process that started it (PAGE_RUN_KEY) is alive. A run cut short by a
    dashboard restart leaves a "running" row behind for hours, so the runs
    table alone can't say this."""
    return _alive(parse(raw).get("pid"))


def threshold(cfg, pace: int) -> int:
    """Top up below this many unchecked companies: about backlog_days of
    the Scout's recent pace, never less than min_backlog."""
    return max(int(cfg.min_backlog), int(round(cfg.backlog_days * pace)))


def describe(saved: dict) -> tuple[str, str]:
    """(state, one line) for status displays. state is off, running,
    waiting, paused or blocked."""
    state = saved.get("state") or "off"
    if state == "off":
        return "off", "off (set discovery.auto: true to keep the company list topped up)"
    line = saved.get("reason") or state
    last = saved.get("last") or {}
    if last.get("at"):
        line += (f". Last run {last['at'][:16].replace('T', ' ')} UTC: "
                 f"{last.get('searches', 0)} searches, {last.get('new', 0)} new companies"
                 + (f", ${last['cost']:.2f}" if last.get("cost") else ""))
    return state, line


class AutoDiscovery:
    """One per running loop: decides each heartbeat whether to start a run,
    and runs at most one at a time in the background."""

    def __init__(self):
        self.task: asyncio.Task | None = None

    async def _save(self, store, saved: dict, **fields) -> dict:
        record = {**saved, **fields, "updated_at": _stamp()}
        try:
            await store.set_setting(AUTO_KEY, json.dumps(record))
        except Exception as e:
            logger.debug(f"Could not save discovery status: {e}")
        return record

    async def tick(self, state, config) -> None:
        """Called once a heartbeat. Never raises."""
        try:
            await self._tick(state, config)
        except Exception as e:
            logger.warning(f"Automatic discovery check failed: {type(e).__name__}: {e}")

    async def _tick(self, state, config) -> None:
        from sellbird.collectors.discover import (
            DEFAULT_PROVIDER, PROVIDERS, build_queries, preset_problem)
        from sellbird.config import load_env

        cfg = config.discovery
        saved = parse(await state.get_setting(AUTO_KEY))
        if not cfg.auto:
            if saved.get("state", "off") != "off":
                await self._save(state, saved, state="off", reason="")
            return
        if self.task and not self.task.done():
            return
        if saved.get("retry_at", 0) > _now():
            return

        if await state.get_setting(AUTO_PAUSED_KEY):
            if saved.get("state") != "paused":
                await self._save(state, saved, state="paused", reason=PAUSED_REASON)
            return
        if page_run_going(await state.get_setting(PAGE_RUN_KEY)):
            await self._save(state, saved, state="waiting",
                             reason="waiting: a run started from the Discover page is going")
            return

        provider_key = (cfg.provider or await state.get_setting("discovery_provider")
                        or DEFAULT_PROVIDER)
        provider = PROVIDERS.get(provider_key)
        if provider is None:
            await self._blocked(state, saved, f"unknown discovery source {provider_key!r}")
            return
        missing = provider.missing_keys(load_env().model_dump())
        if missing:
            await self._blocked(state, saved, f"{provider.label} needs "
                                f"{', '.join(k.upper() for k in missing)} in .env")
            return

        presets = list(cfg.presets) or [""]
        index = int(saved.get("preset_index", 0)) % len(presets)
        preset = presets[index]
        problem = preset_problem(preset, provider_key, config=config) if preset else ""
        if problem:
            await self._blocked(state, saved, f"preset {preset!r}: {problem}")
            return
        plan = build_queries(config, depth=cfg.depth, limit=cfg.limit, preset=preset)
        signature = plan_signature(plan)
        cursor = int(saved.get("cursor", 0)) if saved.get("signature") == signature else 0

        if cursor >= len(plan):
            # This list is done: on to the next preset, or rest a while.
            if index + 1 < len(presets):
                await self._save(state, saved, preset_index=index + 1, cursor=0,
                                 signature="", state="waiting",
                                 reason=f"waiting: moving on to the next search list")
                return
            done_at = float(saved.get("round_done_at") or _now())
            again = done_at + cfg.repeat_after_days * 86400
            if _now() < again:
                await self._save(state, saved, round_done_at=done_at, state="waiting",
                                 reason="waiting: every search has run; starting the list "
                                        f"again on {_stamp(again)[:10]}")
                return
            index, cursor = 0, 0
            saved = {**saved, "preset_index": 0, "round_done_at": None}
            preset = presets[0]
            plan = build_queries(config, depth=cfg.depth, limit=cfg.limit, preset=preset)
            signature = plan_signature(plan)

        backlog = await unchecked_companies(state)
        pace = await checked_last_day(state)
        below = threshold(cfg, pace)
        if backlog >= below:
            await self._save(state, saved, state="waiting", backlog=backlog, threshold=below,
                             reason=f"waiting: {backlog:,} companies not checked yet; "
                                    f"tops up below {below:,}")
            return

        chunk = plan[cursor:cursor + cfg.queries_per_run]
        estimate = provider.estimate(chunk)
        left = cfg.max_daily_spend - await spent_today(state)
        if estimate > 0 and estimate > left:
            why = (f"{provider.label} would cost ${estimate:.2f} for the next "
                   f"{len(chunk)} searches")
            why += (f", over today's ${cfg.max_daily_spend:.2f} cap"
                    if cfg.max_daily_spend else
                    "; set discovery.max_daily_spend or pick a free source")
            await self._blocked(state, saved, why)
            return

        saved = await self._save(
            state, saved, state="running", provider=provider_key, signature=signature,
            preset_index=index, cursor=cursor, backlog=backlog, threshold=below,
            retry_at=0, run_pid=os.getpid(),
            reason=(f"running: searches {cursor + 1}-{cursor + len(chunk)} of {len(plan)} "
                    f"with {provider.label} ({backlog:,} companies waiting, tops up "
                    f"below {below:,})"))
        logger.info(f"Discovery: starting {len(chunk)} searches with {provider.label} "
                    f"({backlog} companies not checked yet).")
        self.task = asyncio.create_task(
            self._run(state, config, provider_key, chunk, max(left, 0.0), saved, len(plan)))

    async def _blocked(self, state, saved: dict, why: str) -> None:
        if saved.get("reason") != f"can't run: {why}":
            logger.warning(f"Automatic discovery can't run: {why}")
        await self._save(state, saved, state="blocked", reason=f"can't run: {why}",
                         retry_at=_now() + RETRY_AFTER_PROBLEM)

    async def _run(self, state, config, provider_key, chunk, budget, saved, total) -> None:
        from sellbird.pipeline import run_prospecting

        paused = False
        run = asyncio.ensure_future(
            run_prospecting(state, config, provider_key, chunk, max_spend=budget))
        try:
            # Pause on the Discover page stops the run within a few seconds,
            # however long the heartbeat sleeps.
            while not run.done():
                await asyncio.wait({run}, timeout=WATCH_SECONDS)
                if not run.done() and await state.get_setting(AUTO_PAUSED_KEY):
                    run.cancel()
                    paused = True
                    break
            report = await run
            found = report.discover or {}
        except asyncio.CancelledError:
            if not paused:
                run.cancel()  # the loop is shutting down
                raise
            # Where it got to isn't known; the chunk runs again on Resume.
            found = {"queries_ran": 0, "stopped": "paused"}
            report = None
        except Exception as e:
            logger.exception("automatic discovery run failed")
            found = {"queries_ran": 0, "stopped": str(e)}
            report = None
        ran = int(found.get("queries_ran") or 0)
        stopped = str(found.get("stopped") or "")
        latest = parse(await state.get_setting(AUTO_KEY))
        cursor = int(saved.get("cursor", 0)) + ran
        last = {"at": _stamp(), "searches": ran, "new": int(found.get("new_companies") or 0),
                "found": int(found.get("found") or 0),
                "cost": float(found.get("actual_cost") or 0.0),
                "stopped": stopped,
                "errors": [str(e)[:200] for e in (report.errors if report else [])][:3]}
        fields = {"cursor": cursor, "last": last, "run_pid": 0}
        if paused:
            fields = {"state": "paused", "reason": PAUSED_REASON, "run_pid": 0}
        elif stopped and not ran:
            fields.update(state="blocked", reason=f"can't run: {stopped}",
                          retry_at=_now() + RETRY_AFTER_PROBLEM)
        elif stopped:
            fields.update(state="waiting", reason=f"waiting: last run stopped early ({stopped})",
                          retry_at=_now() + RETRY_AFTER_PROBLEM)
        else:
            fields.update(state="waiting",
                          reason=f"waiting: {cursor:,} of {total:,} searches done")
        await self._save(state, {**latest, **{k: v for k, v in saved.items()
                                             if k in ("signature", "preset_index")}},
                         **fields)
        if paused:
            logger.info("Discovery: paused from the Discover page; the run was stopped.")
            return
        logger.info(f"Discovery: {ran} searches, {last['new']} new companies"
                    + (f", stopped: {stopped}" if stopped else "") + ".")
