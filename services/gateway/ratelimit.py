"""
Redis sliding-window rate limiting (spec §8 Service 1: "Enforce per-account
and per-IP rate limiting using Redis token-bucket or sliding-window
counters"). This replaces the gateway's earlier in-process deque, which was
per-PROCESS state and therefore wrong the moment a second gateway replica
exists — the counters now live where the spec puts all gateway state.

ALGORITHM — sliding-window log, one sorted set per subject:

    ZREMRANGEBYSCORE key -inf (now - window)   drop what aged out
    ZCARD key                                  how many remain in the window
    ZADD key now <unique>                      admit (only if under the limit)
    EXPIRE key window+1                        idle subjects self-clean

A true sliding window, not a fixed bucket: the limit can never be doubled by
straddling a window boundary. Cost is one small ZSET per active IP/account,
bounded by the limit itself.

TWO SUBJECTS, checked at different points in the pipeline (see main.py):
  - per IP      — the anonymous shield, checked BEFORE the token is verified
                  so a flood of forged tokens is throttled rather than
                  costing an RSA verification each.
  - per account — checked immediately AFTER identity is established, which is
                  the earliest point it CAN be: there is no account before
                  the token is read, and it must still be before the request
                  reaches an upstream.

Two round trips (read, then admit) rather than one atomic script: a rejected
request must NOT add a token, or a client already over the limit would keep
pushing its own window forward and stay locked out past the window. The
non-atomic read-then-admit lets a burst of exactly-concurrent requests slip a
few over the limit — the same benign race the in-process version had, and the
right trade against shipping a Lua script the test fake would have to
emulate.

FAIL-OPEN on a Redis outage: rate limiting is a protection, not an
authorization decision, and taking the entire platform offline because the
counter store blinked is a worse failure than briefly unthrottled traffic.
(Webhook dedup, which IS a correctness decision, fails CLOSED — see
webhooks.py.)
"""
from __future__ import annotations

import logging
import time
import uuid

import redis.exceptions

from creditflow_common import config

import store

logger = logging.getLogger("gateway.ratelimit")

# 0 disables either limit independently.
IP_LIMIT_PER_WINDOW = int(config.env("GATEWAY_RATE_LIMIT_PER_MINUTE", "120"))
ACCOUNT_LIMIT_PER_WINDOW = int(config.env("GATEWAY_ACCOUNT_RATE_LIMIT_PER_MINUTE", "600"))
WINDOW_SECONDS = int(config.env("GATEWAY_RATE_LIMIT_WINDOW_SECONDS", "60"))

IP_KEY = "gateway:ratelimit:ip:{subject}"
ACCOUNT_KEY = "gateway:ratelimit:account:{subject}"


async def _allow(key: str, limit: int) -> bool:
    """True if this request fits inside the window for `key`."""
    if limit <= 0:
        return True
    redis_client = store.get_redis()
    now = time.time()
    try:
        pipe = redis_client.pipeline()
        pipe.zremrangebyscore(key, "-inf", now - WINDOW_SECONDS)
        pipe.zcard(key)
        _, in_window = await pipe.execute()
        if in_window >= limit:
            return False
        admit = redis_client.pipeline()
        admit.zadd(key, {f"{now:.6f}:{uuid.uuid4().hex}": now})
        admit.expire(key, WINDOW_SECONDS + 1)
        await admit.execute()
        return True
    except (redis.exceptions.RedisError, OSError):
        logger.warning("rate-limit store unavailable; failing open for %s", key, exc_info=True)
        return True


async def allow_ip(ip: str) -> bool:
    return await _allow(IP_KEY.format(subject=ip), IP_LIMIT_PER_WINDOW)


async def allow_account(account_id: str) -> bool:
    return await _allow(ACCOUNT_KEY.format(subject=account_id), ACCOUNT_LIMIT_PER_WINDOW)
