"""
RabbitMQ consumer for the Scheduler service.

Consumes the durable queue `scheduler.content_events` on the `content_events`
exchange — the queue the Content service PRE-DECLARED for us (its events.py
binds ALL five content.* keys there), so the full lifecycle history of every
content item is already waiting when this consumer comes up. We re-declare on
connect (idempotent), same contract as every other service.

Spec: the Scheduler consumes content.created "so the Scheduler Service knows
what is available to schedule". We take that one step further and mirror the
whole lifecycle into the content_refs read model:

  - content.created / content.updated / content.approved / content.published
    -> upsert the ref (id, account, status, version, title, image_url).
    Schedule creation validates against this — content must exist, be ours,
    and be approved (see routes). Last-write-wins; payloads always carry the
    full current state, so replays and mild reordering converge.
  - content.deleted -> drop the ref AND cancel every still-pending schedule
    pointing at it (firing a schedule for deleted content would emit a
    content.scheduled event pointing at nothing).

Idempotency (spec §7 — consumers must survive at-least-once redelivery):
`already_processed(db, event_id)` + the processed_events table dedupe broker
redeliveries; the event row and the read-model/cancel writes commit in the
SAME transaction, so either both exist or neither does. Upserts are naturally
idempotent on top, so even a fresh event_id for the same state converges.

The consumer runs as a daemon thread (see main.py); the loop in `run()`
reconnects forever so a broker restart doesn't kill the service. A raising
handler makes the shared consumer retry (bounded) and then dead-letter.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from creditflow_common import rabbitmq
from creditflow_common.idempotency import already_processed

import database
from models import ContentRef, ScheduledPost

logger = logging.getLogger("scheduler.consumer")

EXCHANGE = "content_events"           # the Content service publishes here
QUEUE = "scheduler.content_events"    # pre-declared for us by the Content service
ROUTING_KEYS = [
    "content.created",
    "content.updated",
    "content.approved",
    "content.published",
    "content.deleted",
]


def handle_event(routing_key: str, data: dict, event_id: str) -> None:
    db = database.SessionLocal()
    try:
        if already_processed(db, event_id):
            db.commit()
            logger.info("skipping already-processed event %s (%s)", event_id, routing_key)
            return
        if routing_key == "content.deleted":
            _content_deleted(db, data)
        else:
            _upsert_ref(db, data)
        db.commit()  # processed_events row + read-model writes land atomically
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _upsert_ref(db, data: dict) -> None:
    content_id = data.get("content_id")
    account_id = data.get("account_id")
    if not content_id or not account_id:
        logger.warning("content event without content_id/account_id — dropping: %s", data)
        return
    ref = db.get(ContentRef, content_id)
    if ref is None:
        ref = ContentRef(content_id=content_id, account_id=account_id)
        db.add(ref)
    ref.account_id = account_id
    ref.status = data.get("status") or ref.status
    ref.version = data.get("version") or ref.version
    ref.title = data.get("title")
    ref.image_url = data.get("image_url")


def _content_deleted(db, data: dict) -> None:
    content_id = data.get("content_id")
    if not content_id:
        logger.warning("content.deleted without content_id — dropping: %s", data)
        return
    ref = db.get(ContentRef, content_id)
    if ref is not None:
        db.delete(ref)
    pending = db.scalars(
        select(ScheduledPost)
        .where(ScheduledPost.content_id == content_id, ScheduledPost.status == "pending")
    ).all()
    for row in pending:
        row.status = "cancelled"
    if pending:
        logger.info("content %s deleted — cancelled %d pending schedule(s)",
                    content_id, len(pending))


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
