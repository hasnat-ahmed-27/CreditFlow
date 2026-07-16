"""
Redis pub/sub token fan-out + replay buffer + cancellation flags.

Why the token stream goes THROUGH Redis instead of straight from the
OpenRouter call to the client:

  1. Decoupled producer/consumer — the upstream OpenRouter stream is consumed
     exactly once by the worker task, no matter how many parties watch it.
     The spec's API Gateway (service 1) re-streams SSE to the frontend "via
     Redis pub/sub subscription": this service and the Gateway (and any
     reconnecting browser tab) can all attach to the same `ai:stream:{job_id}`
     channel without each opening their own paid OpenRouter call.
  2. Client disconnects don't kill the generation — if the SSE consumer drops,
     the worker keeps draining OpenRouter, so the final response is still
     persisted and ai.generation_completed still fires with real token counts
     (the account IS being billed upstream either way).
  3. Late subscribers are safe — every chunk is also RPUSHed to a bounded-TTL
     Redis list (`ai:buffer:{job_id}`). A subscriber first replays the list,
     then follows the live channel, deduping by the per-chunk `seq` number.
     Without the buffer, tokens published in the gap between POST /generations
     returning and the client opening the stream would be lost forever
     (pub/sub has no memory).

Message envelope (JSON on both the channel and the list):
  {"seq": n, "type": "token", "content": "..."}          — one streamed chunk
  {"seq": n, "type": "done", ...token counts...}          — terminal, success
  {"seq": n, "type": "cancelled", ...token counts...}     — terminal, cancelled
  {"seq": n, "type": "error", "reason": "..."}            — terminal, failed

Cancellation: `ai:cancel:{job_id}` is a plain flag key the cancel endpoint
sets and the worker polls between chunks — the worker owns stopping the
upstream stream and writing the terminal state, so cancel is race-free.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import redis.asyncio as redis

from creditflow_common import config

CHANNEL_KEY = "ai:stream:{job_id}"
BUFFER_KEY = "ai:buffer:{job_id}"
CANCEL_KEY = "ai:cancel:{job_id}"

# The buffer/flag outlive any realistic stream by far, then self-clean;
# the durable record is Postgres, not Redis.
BUFFER_TTL_SECONDS = 60 * 60
CANCEL_TTL_SECONDS = 60 * 60

TERMINAL_TYPES = frozenset({"done", "cancelled", "error"})

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(config.REDIS_URL, decode_responses=True)
    return _client


def _channel(job_id: str) -> str:
    return CHANNEL_KEY.format(job_id=job_id)


def _buffer(job_id: str) -> str:
    return BUFFER_KEY.format(job_id=job_id)


def _cancel(job_id: str) -> str:
    return CANCEL_KEY.format(job_id=job_id)


async def publish_chunk(job_id: str, message: dict) -> None:
    """Buffer first, then broadcast: a subscriber attaching between the two
    sees the chunk in its replay; one attached before sees it live. The `seq`
    field lets it drop the duplicate when both happen."""
    r = get_redis()
    raw = json.dumps(message)
    await r.rpush(_buffer(job_id), raw)
    await r.expire(_buffer(job_id), BUFFER_TTL_SECONDS)
    await r.publish(_channel(job_id), raw)


async def request_cancel(job_id: str) -> None:
    await get_redis().set(_cancel(job_id), "1", ex=CANCEL_TTL_SECONDS)


async def cancel_requested(job_id: str) -> bool:
    return bool(await get_redis().exists(_cancel(job_id)))


async def stream_messages(job_id: str, idle_timeout: float = 300.0) -> AsyncIterator[dict]:
    """Yield every chunk for a job in order, exactly once, then stop at the
    terminal message. Subscribe BEFORE replaying the buffer so no chunk can
    fall between the two; `seq` dedupes the overlap."""
    r = get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(_channel(job_id))
    try:
        last_seq = 0
        for raw in await r.lrange(_buffer(job_id), 0, -1):
            message = json.loads(raw)
            last_seq = message["seq"]
            yield message
            if message["type"] in TERMINAL_TYPES:
                return
        idle = 0.0
        while True:
            item = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if item is None:
                idle += 1.0
                if idle >= idle_timeout:
                    # Producer died without a terminal message (crash between
                    # chunks) — close the stream instead of hanging forever.
                    yield {"seq": last_seq + 1, "type": "error", "reason": "stream idle timeout"}
                    return
                continue
            idle = 0.0
            message = json.loads(item["data"])
            if message["seq"] <= last_seq:
                continue  # already served from the replay buffer
            last_seq = message["seq"]
            yield message
            if message["type"] in TERMINAL_TYPES:
                return
    finally:
        try:
            await pubsub.unsubscribe(_channel(job_id))
            await pubsub.aclose()
        except Exception:  # noqa: BLE001 — teardown must not mask the stream result
            pass
