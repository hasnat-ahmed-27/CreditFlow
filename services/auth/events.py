"""
Domain-event publishing for the auth service.

Publishes to the durable `user_events` topic exchange (publisher confirms +
persistent messages come from creditflow_common.rabbitmq.Publisher).

Routing keys (per spec): user.registered, user.logged_in,
user.password_reset_requested.

The connection is created lazily on first publish and a failure is logged but
does not fail the user-facing request: auth must still work if the broker is
briefly down, and the Notification service is a best-effort consumer. (Where
delivery MUST be guaranteed the spec prescribes the transactional outbox —
that's a Billing-service requirement, not Auth.)
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher

logger = logging.getLogger("auth.events")

EXCHANGE = "user_events"

_publisher: Publisher | None = None


def publish(routing_key: str, payload: dict) -> str | None:
    """Returns the event_id, or None if the broker was unreachable."""
    global _publisher
    try:
        if _publisher is None:
            _publisher = Publisher(exchange=EXCHANGE)
        return _publisher.publish(routing_key, payload)
    except Exception:  # noqa: BLE001 — broker down must not 500 the request
        logger.exception("failed to publish %s to %s", routing_key, EXCHANGE)
        _publisher = None  # force a fresh connection on the next attempt
        return None
