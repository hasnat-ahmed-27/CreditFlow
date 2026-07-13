"""
Domain-event publishing for the usage service — topic exchange
`usage_events`, durable + publisher confirms + persistent messages via
creditflow_common.rabbitmq.Publisher.

Routing keys (per spec): usage.threshold_reached — consumed by the
Notification service (quota alert emails). `usage_events` is also the
exchange the AI service will publish ai.generation_completed on; OUR durable
consumer queue there (`usage.usage_events`) is declared by consumer.run() at
startup — since this service ships before the AI service exists, the queue
is waiting long before the first event, so nothing is lost.

Pre-declared durable queues (same contract as the other services): the shared
Publisher sends with mandatory=True, so every routing key needs at least one
bound queue or the publish bounces as unroutable. We declare the queue our
(future) Notification consumer will read from so no event is dropped before
it exists.

Posture: best-effort like auth/user/credits — a broker outage is logged but
never fails the request or the consumer.
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("usage.events")

EXCHANGE = "usage_events"

PREDECLARED_QUEUES: dict[str, list[str]] = {
    "notifications.usage_events": ["usage.threshold_reached"],
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
