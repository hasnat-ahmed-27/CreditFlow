"""
Domain-event publishing for the Content service — topic exchange
`content_events` (exchange-per-domain rule), durable + publisher confirms +
persistent messages via creditflow_common.rabbitmq.Publisher.

Routing keys: content.created and content.updated (the spec's contract — "so
the Scheduler Service knows what is available to schedule"), plus the
lifecycle signals content.approved / content.published (dedicated keys so a
consumer that only cares about workflow gates doesn't have to sift every
edit) and content.deleted (so the Scheduler can drop schedules pointing at
removed content). Every status transition emits exactly ONE of
updated/approved/published — payloads carry previous_status -> status either
way, so content.updated remains a full catch-all for state watchers.

Pre-declared durable queue (same contract as the other services): the shared
Publisher sends with mandatory=True, so every routing key needs at least one
bound queue or the publish bounces as unroutable.
  - `scheduler.content_events` — the future Scheduler service (service 9)
    consumes content.created per spec; binding ALL our keys now means the
    full lifecycle history queues up for it from day one, and none of our
    publishes are ever unroutable.

Posture: best-effort like the other services — a broker outage is logged but
never fails the request; the durable truth is already in Postgres.
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("content.events")

EXCHANGE = "content_events"

PREDECLARED_QUEUES: dict[str, list[str]] = {
    "scheduler.content_events": [
        "content.created",
        "content.updated",
        "content.approved",
        "content.published",
        "content.deleted",
    ],
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
    except Exception:  # noqa: BLE001 — broker down must not kill the request
        logger.exception("failed to publish %s to %s", routing_key, EXCHANGE)
        _publisher = None  # force a fresh connection on the next attempt
        return None
