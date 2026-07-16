"""
Redis live counters — the FAST half of the metering split.

One key per account per calendar month holds SUM(total_tokens) of that
account's usage_ledger rows for the period:

    usage:tokens:{account_id}:{YYYY-MM}

The quota pre-check (called by the AI service before EVERY generation) is a
single GET on this key — never a Postgres aggregate on the hot path. The
counter is a disposable cache of the ledger: a missing key (Redis restart,
TTL expiry) or a drifted value is healed by rewriting it from the Postgres
sum (see quota.py / consumer.py), so Redis being wrong is never worse than
one slow rebuild. Postgres is the truth; Redis is the speed.
"""
from __future__ import annotations

import redis

from creditflow_common import config

TOKENS_KEY = "usage:tokens:{account_id}:{period}"
# Counters self-clean ~2 months after their period ends; history lives in
# the ledger, not in Redis.
COUNTER_TTL_SECONDS = 70 * 24 * 60 * 60

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    return _client


def _key(account_id: str, period: str) -> str:
    return TOKENS_KEY.format(account_id=account_id, period=period)


def get_tokens(account_id: str, period: str) -> int | None:
    """Current counter value, or None if the key is missing (never written,
    expired, or Redis was flushed) — the caller rebuilds from Postgres."""
    val = get_redis().get(_key(account_id, period))
    return int(val) if val is not None else None


def set_tokens(account_id: str, period: str, tokens: int) -> None:
    """Authoritative overwrite from the Postgres-derived sum — this IS the
    reconciliation: any drift the counter accumulated is erased here."""
    get_redis().set(_key(account_id, period), int(tokens), ex=COUNTER_TTL_SECONDS)
