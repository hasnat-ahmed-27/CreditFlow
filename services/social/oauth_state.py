"""
CSRF `state` store for the LinkedIn OAuth flow — Redis, one-time, short TTL.

POST /connections/linkedin/start mints a random state, stores WHO asked
(account_id + user_id) under `social:oauth_state:{state}` with a 10-minute
TTL, and sends the state to LinkedIn. The callback must present a state that
(a) exists — unknown/expired states are rejected, (b) belongs to the caller's
account — a state minted by tenant A cannot complete tenant B's connect, and
(c) has never been used — `consume` deletes atomically with the read, so a
replayed callback fails. That is the standard authorization-code CSRF
defense: an attacker cannot splice their own `code` into a victim's session
without also holding a state minted FOR that victim's account.

Same lazy-client pattern as scheduler/tasks.get_redis — tests monkeypatch
get_redis to fakeredis.
"""
from __future__ import annotations

import json
import secrets

from creditflow_common import config

STATE_TTL_SECONDS = 600
_PREFIX = "social:oauth_state:"

_redis = None


def get_redis():
    """Lazy sync Redis client (tests monkeypatch this)."""
    global _redis
    if _redis is None:
        import redis

        _redis = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis


def issue(account_id: str, user_id: str | None) -> str:
    """Mint and store a fresh one-time state; returns it."""
    state = secrets.token_urlsafe(32)
    get_redis().set(
        _PREFIX + state,
        json.dumps({"account_id": account_id, "user_id": user_id}),
        ex=STATE_TTL_SECONDS,
    )
    return state


def consume(state: str) -> dict | None:
    """Read-and-delete in one pipeline (one-time use). None if unknown/expired."""
    key = _PREFIX + state
    raw, _ = get_redis().pipeline().get(key).delete(key).execute()
    return json.loads(raw) if raw else None
