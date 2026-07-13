"""
RabbitMQ consumer for the usage service.

Consumes from the `usage_events` exchange on the durable queue
`usage.usage_events`. The AI service (service 7, not built yet) will publish
here; because run() declares the queue (durable, DLX-bound) the moment THIS
service starts, every ai.generation_completed emitted later queues up for us
from day one — same "queue exists before the producer/consumer counterpart"
contract Billing and Credits use:

  ai.generation_completed -> append a usage_ledger row (tokens/cost/model),
                             reconcile the Redis counter, emit
                             usage.threshold_reached on 80%/100% crossings

Expected payload (contract for the future AI service):
  { "account_id": "...", "user_id": "...", "job_id": "...",
    "model": "openai/gpt-4o-mini",
    "input_tokens": 123, "output_tokens": 456, "total_tokens": 579,
    "cost_usd": 0.0123 }
(total_tokens optional — defaults to input+output.)

Idempotency (spec §7 — consumers must survive at-least-once redelivery),
two independent layers, same as Credits:
  1. `already_processed(db, event_id)` + the processed_events table dedupe
     broker redeliveries: the event row and the ledger row commit in the
     SAME transaction, so either both exist or neither does.
  2. A business-key guard on the ledger itself (job_id) — covers the AI
     service re-emitting the same generation under a FRESH event_id.

Redis ordering: the ledger row commits FIRST, then the counter is overwritten
with the Postgres-derived sum (reconcile-on-write). A crash between the two
leaves a stale counter, which the skip-path reconcile on redelivery — or the
next write/read-miss — heals. Redis errors are logged, never raised: the
durable truth is already committed, and raising would only re-run a no-op.

The consumer runs as a daemon thread (see main.py); the loop in `run()`
reconnects forever so a broker restart doesn't kill the service. A raising
handler makes the shared consumer retry (bounded) and then dead-letter.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from creditflow_common import rabbitmq
from creditflow_common.idempotency import already_processed

import database
import events
import quota
from models import UsageEntry

logger = logging.getLogger("usage.consumer")

EXCHANGE = "usage_events"        # shared domain exchange — AI publishes, we consume
QUEUE = "usage.usage_events"     # declared durable+DLX by run() at startup
ROUTING_KEYS = ["ai.generation_completed"]


def handle_event(routing_key: str, data: dict, event_id: str) -> None:
    db = database.SessionLocal()
    try:
        account_id = data.get("account_id")
        period = quota.current_period()
        if already_processed(db, event_id):
            db.commit()
            logger.info("skipping already-processed event %s (%s)", event_id, routing_key)
            # Heal the crash-between-commit-and-Redis window: the ledger row
            # exists, so make sure the counter reflects it.
            if account_id:
                _reconcile_counter(db, account_id, period)
            return
        crossed: list[int] = []
        if routing_key == "ai.generation_completed":
            crossed = _record_usage(db, data, period)
        db.commit()  # processed_events row + ledger row land atomically
        # Redis + events only after the commit: never announce (or count) a
        # rolled-back write.
        if account_id:
            used = _reconcile_counter(db, account_id, period)
            for pct in crossed:
                events.publish("usage.threshold_reached", {
                    "account_id": account_id,
                    "threshold_percent": pct,
                    "used_tokens": used if used is not None else 0,
                    "quota_tokens": quota.quota_for(account_id),
                    "period": period,
                })
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _record_usage(db, data: dict, period: str) -> list[int]:
    """Append the ledger row; returns the threshold percents this event
    crossed (computed from Postgres sums, not the Redis counter)."""
    account_id = data.get("account_id")
    job_id = data.get("job_id")
    if not account_id or not job_id:
        logger.warning("ai.generation_completed without account_id/job_id — dropping: %s", data)
        return []

    # Business-key guard: one ledger row per generation job, ever.
    existing = db.scalar(select(UsageEntry).where(UsageEntry.job_id == job_id))
    if existing is not None:
        logger.info("job %s already recorded (entry %s) — skipping", job_id, existing.id)
        return []

    input_tokens = int(data.get("input_tokens") or 0)
    output_tokens = int(data.get("output_tokens") or 0)
    total_tokens = int(data.get("total_tokens") or (input_tokens + output_tokens))

    before = quota.tokens_used_db(db, account_id, period)
    db.add(UsageEntry(
        account_id=account_id,
        job_id=job_id,
        model=data.get("model") or "unknown",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=_as_decimal(data.get("cost_usd")),
        period=period,
        created_by_user_id=data.get("user_id"),
    ))
    logger.info("recorded %d tokens for %s (job %s, model %s)",
                total_tokens, account_id, job_id, data.get("model"))
    return quota.crossed_thresholds(before, before + total_tokens, quota.quota_for(account_id))


def _as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        # Via str so float artifacts (0.1 + 0.2) don't leak into the ledger.
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        logger.warning("unparseable cost_usd %r — storing NULL", value)
        return None


def _reconcile_counter(db, account_id: str, period: str) -> int | None:
    """Best-effort reconcile-on-write; returns the Postgres-true count, or
    None if Redis was unreachable (the truth is committed either way)."""
    try:
        return quota.reconcile(db, account_id, period)
    except Exception:  # noqa: BLE001 — Redis down must not dead-letter the event
        logger.exception("could not reconcile Redis counter for %s %s", account_id, period)
        return None


def run() -> None:
    """Blocking consume loop with reconnect — target for the daemon thread."""
    while True:
        try:
            rabbitmq.consume(
                exchange=EXCHANGE,
                queue=QUEUE,
                routing_keys=ROUTING_KEYS,
                handler=handle_event,
            )
        except Exception:  # noqa: BLE001 — broker hiccup: log, back off, reconnect
            logger.exception("consumer connection lost — retrying in 5s")
            time.sleep(5)
