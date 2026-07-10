"""
Redis-backed session store + login rate limiter.

Why jti-in-Redis: RS256 JWTs are stateless — any service can verify them with
the public key, but nobody can "un-issue" one. Storing each active token's jti
in Redis with TTL = token expiry gives us a revocation switch: the Gateway (and
this service) treat a token as valid only while `session:{jti}` exists, and the
Admin service can list/revoke active sessions by scanning the same keys.

Rate limiting: sliding window over a Redis sorted set — each attempt is a
member scored by its timestamp; old entries are trimmed, and the remaining
count is compared against the limit. Unlike a fixed INCR/EXPIRE window this
can't be gamed by bursting at a window boundary.
"""
from __future__ import annotations

import json
import time
import uuid

import redis

from creditflow_common import config

SESSION_PREFIX = "session:"

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    return _client


# --- active-session (jti) store ---

def store_session(jti: str, user_id: str, account_id: str, role: str, ttl_seconds: int) -> None:
    payload = json.dumps(
        {"user_id": user_id, "account_id": account_id, "role": role, "issued_at": int(time.time())}
    )
    get_redis().set(SESSION_PREFIX + jti, payload, ex=ttl_seconds)


def session_exists(jti: str) -> bool:
    return get_redis().exists(SESSION_PREFIX + jti) == 1


def revoke_session(jti: str) -> bool:
    """Delete the jti — the token is invalid everywhere from this moment."""
    return get_redis().delete(SESSION_PREFIX + jti) == 1


# --- sliding-window rate limiter ---

def rate_limit_exceeded(key: str, limit: int, window_seconds: int) -> bool:
    """
    Record one attempt under `key` and return True if the limit was already
    reached within the window (the blocked attempt is not recorded).
    """
    r = get_redis()
    zkey = f"ratelimit:{key}"
    now = time.time()
    r.zremrangebyscore(zkey, 0, now - window_seconds)
    if r.zcard(zkey) >= limit:
        return True
    r.zadd(zkey, {f"{now}:{uuid.uuid4().hex[:8]}": now})
    r.expire(zkey, window_seconds)
    return False
