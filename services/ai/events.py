"""
Domain-event publishing for the AI service — topic exchange `usage_events`
(the domain exchange ai.* lives on, per the platform's exchange-per-domain
rule), durable + publisher confirms + persistent messages via
creditflow_common.rabbitmq.Publisher.

Routing keys (per spec): ai.generation_completed (cost/tokens — consumed by
Usage's already-running `usage.usage_events` queue, payload contract in that
service's consumer.py) and ai.generation_failed (error reason).

Pre-declared durable queues (same contract as the other services): the shared
Publisher sends with mandatory=True, so every routing key needs at least one
bound queue or the publish bounces as unroutable. Declaring a consumer's
queue HERE also means its events accumulate durably from day one, so a
consumer that is down (or not yet deployed) misses nothing.
  - `content.usage_events` — the Content service (service 8) consumes
    ai.generation_completed to create drafts.
  - `credits.usage_events` — the Credits service (service 5) consumes the
    same key to debit the account for the tokens spent (spec §10). This one
    is the money path: if Credits is down, the completion must WAIT in its
    queue, not vanish.
  - `notifications.usage_events` — already declared by the Usage service for
    usage.threshold_reached; re-declaring is idempotent and ADDS the
    ai.generation_failed binding, so failure alerts land in the queue the
    Notification service is already reading.
(ai.generation_completed is routable from day one because Usage's own
consumer queue binds it at Usage startup.)

Posture: best-effort like the other services — a broker outage is logged but
never fails the request; the durable truth is already in Postgres.
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("ai.events")

EXCHANGE = "usage_events"

PREDECLARED_QUEUES: dict[str, list[str]] = {
    "content.usage_events": ["ai.generation_completed"],
    "credits.usage_events": ["ai.generation_completed"],
    "notifications.usage_events": ["ai.generation_failed"],
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
    except Exception:  # noqa: BLE001 — broker down must not kill the worker
        logger.exception("failed to publish %s to %s", routing_key, EXCHANGE)
        _publisher = None  # force a fresh connection on the next attempt
        return None
