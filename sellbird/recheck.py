"""Check guessed emails again once a verifier works.

A prospect found while the verifier was down, out of credits or missing keeps
a guessed email, and a guess is never sent. Nothing used to look at it again,
so a day's outage left those people stuck for good. This runs a small batch
each cycle while a verifier is working and moves each guess to verified,
risky or invalid, or stamps it to wait before the next try.
"""

import logging

from sellbird import verifier_health
from sellbird.integrations.email_finder import reoon_credits, verify_published_address

logger = logging.getLogger("sellbird.recheck")

# Per cycle: small, so one cycle never spends a day's credits at once.
RECHECK_BATCH = 10


async def recheck_guessed_emails(state, env, limit: int = RECHECK_BATCH) -> dict:
    """Re-check up to ``limit`` guessed emails. Returns counts by result."""
    counts: dict[str, int] = {}
    if not verifier_health.ready(env):
        return counts
    reoon_key = (getattr(env, "reoon_api_key", "") or "").strip() if env else ""
    if reoon_key and verifier_health.configured(env) == ["reoon"]:
        # The only verifier is Reoon: its free balance call says whether any
        # credits are left (and when they're back), so none is wasted.
        left = await reoon_credits(reoon_key)
        if left is not None:
            limit = min(limit, left)
    if limit <= 0:
        return counts

    for row in await state.recheckable_emails(limit):
        failing_before = {p for p in verifier_health.configured(env)
                          if verifier_health.failing(p)}
        try:
            status = await verify_published_address(row["email"], env=env)
        except Exception as e:
            logger.debug(f"Re-check failed for a prospect: {e}")
            break
        newly_failing = {p for p in verifier_health.configured(env)
                         if verifier_health.failing(p)} - failing_before
        if status == "guess" and newly_failing:
            # The verifier broke mid-batch: this one wasn't really checked.
            break
        await state.set_email_check(row["id"], status)
        counts[status] = counts.get(status, 0) + 1

    if counts:
        usable = counts.get("verified", 0) + counts.get("risky", 0)
        logger.info(
            f"Re-checked {sum(counts.values())} guessed email(s): {usable} now usable, "
            f"{counts.get('invalid', 0)} invalid, {counts.get('guess', 0)} still unclear.")
    return counts
