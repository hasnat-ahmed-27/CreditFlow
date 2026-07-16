"""
Active-session store access — the spec's "list active JWT sessions (jti
values) per account by reading Redis directly" and "revoke a session on
demand (delete jti from Redis)".

This reads/deletes the EXACT keys the Auth service writes (see
services/auth/store.py): `session:{jti}` holding JSON
{user_id, account_id, role, issued_at} with TTL = token expiry. Deleting the
key is the platform's revocation switch — the Gateway (and Auth's refresh
path) treat a token as valid only while its key exists, so a revoked session
is invalid everywhere from that moment, before natural expiry.

All Redis I/O for this service lives in this ONE module — same lazy-client
pattern as social/oauth_state.py; tests monkeypatch get_redis to fakeredis.
Listing uses SCAN (never KEYS): the session keyspace is small (one key per
live access token) and this is an admin console, not a hot path.
"""
from __future__ import annotations

import json
import time

from creditflow_common import config

SESSION_PREFIX = "session:"

_redis = None


def get_redis():
    """Lazy sync Redis client (tests monkeypatch this)."""
    global _redis
    if _redis is None:
        import redis

        _redis = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis


def _session_dict(jti: str, raw: str, ttl: int) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = {}
    issued_at = data.get("issued_at")
    return {
        "jti": jti,
        "user_id": data.get("user_id"),
        "account_id": data.get("account_id"),
        "role": data.get("role"),
        "issued_at": issued_at,
        "age_seconds": max(int(time.time()) - issued_at, 0) if issued_at else None,
        # TTL left before the token's natural expiry (-1 = no TTL set).
        "expires_in_seconds": ttl if ttl >= 0 else None,
    }


def list_sessions(account_id: str | None = None, user_id: str | None = None) -> list[dict]:
    """All live sessions, optionally filtered by account and/or user."""
    r = get_redis()
    out: list[dict] = []
    for key in r.scan_iter(match=SESSION_PREFIX + "*"):
        raw = r.get(key)
        if raw is None:  # expired between SCAN and GET
            continue
        session = _session_dict(key[len(SESSION_PREFIX):], raw, r.ttl(key))
        if account_id is not None and session["account_id"] != account_id:
            continue
        if user_id is not None and session["user_id"] != user_id:
            continue
        out.append(session)
    out.sort(key=lambda s: s["issued_at"] or 0, reverse=True)
    return out


def get_session(jti: str) -> dict | None:
    """One live session by jti, or None if unknown/expired."""
    r = get_redis()
    raw = r.get(SESSION_PREFIX + jti)
    if raw is None:
        return None
    return _session_dict(jti, raw, r.ttl(SESSION_PREFIX + jti))


def revoke_session(jti: str) -> bool:
    """Delete the jti — the token is invalid everywhere from this moment."""
    return get_redis().delete(SESSION_PREFIX + jti) == 1


def revoke_matching(account_id: str | None = None, user_id: str | None = None) -> int:
    """Force-logout every live session for an account and/or user; returns
    how many were revoked. Backs the suspend routes: suspension without
    killing live tokens would not bite until they expired naturally."""
    revoked = 0
    for session in list_sessions(account_id=account_id, user_id=user_id):
        revoked += int(revoke_session(session["jti"]))
    return revoked
