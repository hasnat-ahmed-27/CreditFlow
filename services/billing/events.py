"""
RabbitMQ publisher wiring for the billing service — topic exchange
`billing_events` (per spec), durable + publisher confirms + persistent
messages via creditflow_common.rabbitmq.Publisher.

IMPORTANT posture difference from the auth/user services: there, a failed
publish is best-effort and the event is simply lost. Here `publish()` is only
ever called by the OUTBOX POLLER — a None return means "broker unreachable",
the outbox row stays unpublished, and the poller retries on the next tick.
The outbox row, not this function, is the durability guarantee.

Pre-declared durable queues (same contract as auth/user): the shared
Publisher sends with mandatory=True, so every routing key must have at least
one bound queue or the publish bounces as unroutable. We declare the queues
our (future) consumers will read from, so no event is dropped before those
services exist:
  - notifications.billing_events  — Notification service (emails/alerts)
  - user.billing_events           — User service bumps plan_tier on invoice.paid
  - credits.billing_events        — Credits service grants on invoice.paid,
                                    claws back on refund.issued (spec: refund
                                    "emit event so Credits Service can claw
                                    back any associated credit grant")
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("billing.events")

EXCHANGE = "billing_events"

PREDECLARED_QUEUES: dict[str, list[str]] = {
    "notifications.billing_events": ["invoice.*", "payment.*", "subscription.*", "refund.*"],
    "user.billing_events": ["invoice.paid"],
    "credits.billing_events": ["invoice.paid", "refund.issued"],
}

_publisher: Publisher | None = None


def publish(routing_key: str, payload: dict, event_id: str | None = None) -> str | None:
    """Publish one event. Returns the event_id on success, None if the broker
    was unreachable (caller — the poller — leaves the outbox row unpublished
    and retries later)."""
    global _publisher
    try:
        if _publisher is None:
            _publisher = Publisher(exchange=EXCHANGE)
            for queue, keys in PREDECLARED_QUEUES.items():
                declare_with_dlx(_publisher._ch, EXCHANGE, queue, keys)
        return _publisher.publish(routing_key, payload, event_id=event_id)
    except Exception:  # noqa: BLE001 — broker down: the outbox retries, never raise into the poller
        logger.exception("failed to publish %s to %s", routing_key, EXCHANGE)
        _publisher = None  # force a fresh connection on the next attempt
        return None
