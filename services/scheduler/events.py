"""
Domain-event publishing for the Scheduler service — topic exchange
`scheduler_events` (exchange-per-domain rule), durable + publisher confirms +
persistent messages via creditflow_common.rabbitmq.Publisher.

ROUTING-KEY CONTRACT: this service publishes exactly ONE key,
`content.scheduled`, emitted by the Celery fire task when a scheduled post's
publish time arrives. That is the spec's wording verbatim ("On a due item,
emit content.scheduled so the Social Publishing Service can proceed — the
Scheduler Service does not call LinkedIn directly"), and the Social
Publishing service's contract lists Consumes: content.scheduled. Payload:
{schedule_id, account_id, content_id, publish_at, fired_at, timezone,
recurrence, fire_count, title, image_url, created_by_user_id} — content_id is
the pointer Social follows to fetch the authoritative post body; title and
image_url are display/convenience mirrors from our read model.

Note the key deliberately does NOT live on the Content service's
content_events exchange: each service publishes only to its own exchange, so
Social binds to `scheduler_events` for the fire signal and (if it wants
lifecycle context) to `content_events` separately.

Pre-declared durable queue (same contract as the other services): the shared
Publisher sends with mandatory=True, so every routing key needs at least one
bound queue or the publish bounces as unroutable.
  - `social.scheduler_events` — the future Social Publishing service
    (service 10) consumes content.scheduled from here; declaring it now means
    fire events queue up durably from day one and no publish is unroutable.

Posture: best-effort like the other services — a broker outage is logged but
never fails the caller; the durable truth (fire_count/last_fired_at/status)
is already committed in Postgres.
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("scheduler.events")

EXCHANGE = "scheduler_events"

# The one key this service emits (see module docstring for the contract).
FIRE_KEY = "content.scheduled"

PREDECLARED_QUEUES: dict[str, list[str]] = {
    "social.scheduler_events": [FIRE_KEY],
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
    except Exception:  # noqa: BLE001 — broker down must not kill the caller
        logger.exception("failed to publish %s to %s", routing_key, EXCHANGE)
        _publisher = None  # force a fresh connection on the next attempt
        return None
