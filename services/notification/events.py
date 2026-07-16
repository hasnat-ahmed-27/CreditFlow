"""
Domain-event publishing for the Notification service — topic exchange
`notification_events` (exchange-per-publisher rule), durable + publisher
confirms + persistent messages via creditflow_common.rabbitmq.Publisher.

ROUTING-KEY CONTRACT (spec verbatim: "Publishes: notification.sent"):
  - `notification.sent` — an email was ACCEPTED by a provider. Emitted only
    after the notification_log row commits (commit-first rule), and never
    for failed/skipped notifications. Payload: {notification_id, event_id
    (the consumed domain event), routing_key (its key), account_id,
    recipient, template, provider, provider_message_id}.

Pre-declared durable queue (same contract as the other services): the shared
Publisher sends with mandatory=True, so every routing key needs at least one
bound queue or the publish bounces as unroutable.
  - `admin.notification_events` — the future Admin/Ops service (service 13)
    consumes ALL domain events into its audit_log; declaring its queue on
    OUR exchange now means notification.sent events accumulate durably from
    day one (the `<consumer>.<exchange>` naming every other pre-declared
    queue follows). Until Admin exists this is also what keeps the publish
    routable.

Posture: best-effort like auth/user/social — a broker outage is logged but
never fails the consumer; the durable truth (the notification_log row) is
already committed in Postgres.
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("notification.events")

EXCHANGE = "notification_events"

SENT_KEY = "notification.sent"

PREDECLARED_QUEUES: dict[str, list[str]] = {
    "admin.notification_events": [SENT_KEY],
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
    except Exception:  # noqa: BLE001 — broker down must not kill the consumer
        logger.exception("failed to publish %s to %s", routing_key, EXCHANGE)
        _publisher = None  # force a fresh connection on the next attempt
        return None
