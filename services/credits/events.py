"""
Domain-event publishing for the credits service — topic exchange
`credits_events`, durable + publisher confirms + persistent messages via
creditflow_common.rabbitmq.Publisher.

Routing keys (per spec): credits.credited, credits.debited,
credits.low_balance — consumed by the Usage service (counter reconciliation)
and the Notification service (low-balance alert emails).

Pre-declared durable queues (same contract as the other services): the shared
Publisher sends with mandatory=True, so every routing key needs at least one
bound queue or the publish bounces as unroutable. We declare the queues our
(future) consumers will read from so no event is dropped before they exist.

Posture: best-effort like auth/user — a broker outage is logged but never
fails the request or the consumer. The transactional outbox is a
Billing-service requirement (spec §7), not ours; our own idempotent consumer
plus Billing's outbox already guarantee the money-critical path.
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("credits.events")

EXCHANGE = "credits_events"

PREDECLARED_QUEUES: dict[str, list[str]] = {
    "notifications.credits_events": ["credits.low_balance"],
    "usage.credits_events": ["credits.*"],
}

_publisher: Publisher | None = None


def publish(routing_key: str, payload: dict) -> str | None:
    """Returns the event_id, or None if the broker was unreachable."""
    global _publisher
    try:
        if _publisher is None:
            _publisher = Publisher(exchange=EXCHANGE)
            for queue, keys in PREDECLARED_QUEUES.items():
                declare_with_dlx(_publisher._ch, EXCHANGE, queue, keys)
        return _publisher.publish(routing_key, payload)
    except Exception:  # noqa: BLE001 — broker down must not 500 the request
        logger.exception("failed to publish %s to %s", routing_key, EXCHANGE)
        _publisher = None  # force a fresh connection on the next attempt
        return None
