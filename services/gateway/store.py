"""
The gateway's Redis handle — spec §8 Service 1, Database Ownership: "None —
stateless. All state lives in Redis (rate-limit counters, webhook dedup keys,
SSE channel subscriptions)."

ASYNC client, unlike the sync `redis.Redis` the other services use. The
gateway's whole request path is async (the SSE passthrough depends on it), so
a blocking Redis call in a middleware would stall the event loop for every
other in-flight request — including a live token stream. Same library, same
URL, `redis.asyncio` instead.

Lazily created and module-global for the same reason proxy.get_client() is:
one connection pool shared across requests. Tests monkeypatch `_client` with
`fakeredis.FakeAsyncRedis`, so the suite never opens a socket.
"""
from __future__ import annotations

import redis.asyncio as aioredis

from creditflow_common import config

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
