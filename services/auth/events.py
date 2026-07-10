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

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("auth.events")

EXCHANGE = "user_events"
# Pre-declared durable queue for the (future) Notification service. The shared
# Publisher sends with mandatory=True, so without at least one bound queue every
# event would bounce as unroutable — and user.registered carries the email
# verification token, which must not be lost. The Notification service will
# consume from this same queue name; until then messages accumulate durably.
NOTIFICATION_QUEUE = "notifications.user_events"

_publisher: Publisher | None = None


def publish(routing_key: str, payload: dict) -> str | None:
    """Returns the event_id, or None if the broker was unreachable."""
    global _publisher
    try:
        if _publisher is None:
            _publisher = Publisher(exchange=EXCHANGE)
            declare_with_dlx(_publisher._ch, EXCHANGE, NOTIFICATION_QUEUE, ["user.*"])
        return _publisher.publish(routing_key, payload)
    except Exception:  # noqa: BLE001 — broker down must not 500 the request
        logger.exception("failed to publish %s to %s", routing_key, EXCHANGE)
        _publisher = None  # force a fresh connection on the next attempt
        return None
