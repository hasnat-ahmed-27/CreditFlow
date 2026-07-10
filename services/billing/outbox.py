"""
outbox.py — the Transactional Outbox (spec §7, THE reliability requirement
of this service).

The problem it solves: "commit the DB change, then publish to RabbitMQ" has
an unfixable gap. If the process crashes — or the broker is down — between
the commit and the publish, the database says the subscription changed but
no consumer ever hears about it. The event is lost forever, and Credits /
User / Notification silently drift out of sync. Publishing BEFORE the commit
is worse: a rolled-back transaction has then announced a change that never
happened. Two systems cannot be updated atomically over a network.

The outbox turns the distributed problem into a local one:

  1. `stage()` inserts the event as a ROW in outbox_events using the caller's
     session, WITHOUT committing. The caller commits its domain change and
     the event row in ONE transaction — the database's own atomicity now
     guarantees "state changed" and "event recorded" are inseparable. Broker
     down? Irrelevant — commit touches only Postgres.
  2. `publish_pending()` (driven by poller.py) reads rows where
     published_at IS NULL, publishes each to RabbitMQ (publisher confirms
     on), and marks it published. If the broker is unreachable it simply
     stops; the rows wait and the next tick retries.

Delivery semantics: at-least-once. A crash after the broker confirmed but
before the mark commits means the row is published again next tick — with
the SAME event_id (the outbox row id), so §7's consumer-side
processed_events dedup makes redelivery harmless. What can never happen is
zero deliveries of a committed change.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

import events
from models import OutboxEvent, utcnow

logger = logging.getLogger("billing.outbox")


def stage(db: Session, routing_key: str, payload: dict) -> OutboxEvent:
    """Add the event row to the CALLER's open transaction — no commit here.
    The caller commits it together with the domain change it describes."""
    row = OutboxEvent(routing_key=routing_key, payload=json.dumps(payload))
    db.add(row)
    return row


def publish_pending(db: Session, limit: int = 100) -> int:
    """Publish unpublished outbox rows in insertion order; mark each row as
    it is confirmed. Safe to call any number of times (a published row is
    never picked up again) and safe to interleave with new stage() commits.
    Returns how many rows were published."""
    query = (
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
        .order_by(OutboxEvent.created_at, OutboxEvent.id)
        .limit(limit)
    )
    if db.get_bind().dialect.name == "postgresql":
        # Multiple service replicas may poll concurrently; SKIP LOCKED lets
        # them split the backlog instead of double-publishing. (SQLite in
        # tests has no row locks — the test poller is single-threaded.)
        query = query.with_for_update(skip_locked=True)

    published = 0
    for row in db.scalars(query):
        row.attempts += 1
        # The outbox row id IS the bus event_id — stable across retries, so
        # consumers' processed_events dedup collapses any redelivery.
        if events.publish(row.routing_key, json.loads(row.payload), event_id=row.id) is None:
            db.commit()  # keep the attempt count; broker is down — stop, next tick retries
            logger.warning("broker unreachable; %d outbox row(s) deferred", 1)
            break
        row.published_at = utcnow()
        # Commit per row: if we crash mid-batch, confirmed rows stay marked
        # and only the in-flight one is re-published (at-least-once).
        db.commit()
        published += 1
    return published
