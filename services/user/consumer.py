"""
RabbitMQ consumer for the user/tenant service.

Consumes `user.registered` from the auth service's `user_events` exchange and
auto-creates the signup's individual account (spec: an individual signup is an
Account of type 'individual' with exactly one member, the Owner).
(`invoice.paid` from Billing will be added to ROUTING_KEYS later to update
plan_tier — Billing doesn't exist yet.)

Idempotency (spec §7 — consumers must survive at-least-once redelivery):
  1. `already_processed(db, event_id)` + the processed_events table dedupe
     broker redeliveries: the event row and the account rows commit in the
     SAME transaction, so either both exist or neither does.
  2. A user_id guard skips creation if the user already owns an individual
     account — covers the producer re-emitting with a FRESH event_id, AND
     the case where Auth's synchronous POST /internal/accounts/individual
     (internal.py) already provisioned the account at signup. Both paths run
     the same provisioning.ensure_individual_account, so whichever arrives
     first wins and the other is a no-op.

The consumer runs as a daemon thread (see main.py); the loop in `run()`
reconnects forever so a broker restart doesn't kill the service.
"""
from __future__ import annotations

import logging
import time

from creditflow_common import rabbitmq
from creditflow_common.idempotency import already_processed

import database
import events
import provisioning

logger = logging.getLogger("user.consumer")

EXCHANGE = "user_events"           # auth's exchange — we consume, never publish here
QUEUE = "user.user_registered"     # our own durable queue (DLX-backed via declare_with_dlx)
ROUTING_KEYS = ["user.registered"]


def handle_event(routing_key: str, data: dict, event_id: str) -> None:
    """Called by creditflow_common.rabbitmq.consume for each delivery.
    Raising makes the shared consumer retry (bounded) and then dead-letter."""
    db = database.SessionLocal()
    try:
        if already_processed(db, event_id):
            db.commit()
            logger.info("skipping already-processed event %s (%s)", event_id, routing_key)
            return
        published: list[tuple[str, dict]] = []
        if routing_key == "user.registered":
            _create_individual_account(db, data, published)
        db.commit()  # processed_events row + domain rows land atomically
        # Publish only after the commit so we never announce a rolled-back write.
        for rk, payload in published:
            events.publish(rk, payload)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _create_individual_account(db, data: dict, published: list[tuple[str, dict]]) -> None:
    user_id = data.get("user_id")
    if not user_id:
        logger.warning("user.registered without user_id — dropping: %s", data)
        return
    account, created = provisioning.ensure_individual_account(db, user_id, data.get("email", ""))
    if created:
        published.append(provisioning.account_created_event(account, user_id))


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
