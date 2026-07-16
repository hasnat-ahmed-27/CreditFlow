"""
Quota arithmetic + Redis<->Postgres reconciliation — the ONLY place the two
stores meet.

Rules:
  - Postgres (usage_ledger) is the truth: `tokens_used_db` is SUM(total_tokens)
    for the period.
  - Redis is the cache read on the hot path: `tokens_used` returns the counter
    and, on a miss, rebuilds it from Postgres (reconcile-on-read). Writers
    reconcile too: the consumer overwrites the counter with the Postgres sum
    after every commit (reconcile-on-write, see consumer.py). Between the two,
    drift never survives past the next read-miss or write.
  - Thresholds fire on the CROSSING (before < line <= after), not on every
    event while already above the line — otherwise every generation after 80%
    would re-alert.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creditflow_common import config

import store
from models import UsageEntry

logger = logging.getLogger("usage.quota")

# Monthly token quota per account. Env-configurable; a per-plan quota lookup
# can replace this once plan tier is consumable (subscription.updated), but
# the spec only requires "its quota" — one number, 80%/100% lines.
DEFAULT_QUOTA_TOKENS = int(config.env("USAGE_QUOTA_TOKENS", "100000"))
THRESHOLD_PERCENTS = (80, 100)


def quota_for(account_id: str) -> int:  # noqa: ARG001 — per-account hook, one policy today
    return DEFAULT_QUOTA_TOKENS


def current_period() -> str:
    """Calendar month, UTC — 'YYYY-MM'. Shared by ledger rows and Redis keys."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def tokens_used_db(db: Session, account_id: str, period: str) -> int:
    """The truth: SUM over the append-only ledger for the period."""
    return int(db.scalar(
        select(func.coalesce(func.sum(UsageEntry.total_tokens), 0))
        .where(UsageEntry.account_id == account_id, UsageEntry.period == period)
    ))


def tokens_used(db: Session, account_id: str, period: str) -> int:
    """Hot-path read: Redis counter, rebuilt from Postgres on a miss so a
    Redis restart/flush degrades to one slow read, never to a wrong answer."""
    cached = store.get_tokens(account_id, period)
    if cached is not None:
        return cached
    used = tokens_used_db(db, account_id, period)
    store.set_tokens(account_id, period, used)
    logger.info("rebuilt Redis counter for %s %s from ledger: %d tokens", account_id, period, used)
    return used


def reconcile(db: Session, account_id: str, period: str) -> int:
    """Force the counter to the Postgres-derived sum; returns it."""
    used = tokens_used_db(db, account_id, period)
    store.set_tokens(account_id, period, used)
    return used


def crossed_thresholds(before: int, after: int, quota: int) -> list[int]:
    """Which of the 80/100 lines this delta crossed (in order). quota <= 0
    means unmetered — no lines to cross."""
    if quota <= 0:
        return []
    return [pct for pct in THRESHOLD_PERCENTS if before < quota * pct / 100 <= after]
