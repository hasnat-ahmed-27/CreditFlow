"""
Domain-event publishing for the Social Publishing service — topic exchange
`social_events` (exchange-per-publisher rule), durable + publisher confirms +
persistent messages via creditflow_common.rabbitmq.Publisher.

ROUTING-KEY CONTRACT (spec verbatim: "Emit post.published (with LinkedIn
post URL/ID) or post.failed (with reason)"):
  - `post.published` — a publish attempt succeeded. Payload carries the
    LinkedIn record downstream services need: linkedin_post_id (the URN) and
    linkedin_post_url, plus job_id, account_id, content_id, schedule_id,
    connection_id, source (manual|scheduled), text_source, image_included,
    and fire_event_id (the consumed content.scheduled event, when scheduled).
  - `post.failed` — a publish attempt concluded in failure. Same identifiers
    plus `error` (the reason). Transient failures in the consumer do NOT emit
    this — they are retried by the broker and only the terminal outcome is
    announced.

Both keys are built by publishing.emit_result from the publish_jobs row, so
the event stream and the audit table always agree.

Pre-declared durable queue (same contract as the other services): the shared
Publisher sends with mandatory=True, so every routing key needs at least one
bound queue or the publish bounces as unroutable.
  - `notifications.social_events` — the future Notification service
    (service 12) consumes post.published / post.failed per its spec contract
    ("send status/alert emails"); declaring it now means publish results
    queue up durably from day one. The Admin service (13) binds `#` on all
    exchanges itself for the audit log, so it needs no pre-declared queue.

Posture: best-effort like the other services — a broker outage is logged but
never fails the caller; the durable truth (the publish_jobs row) is already
committed in Postgres.
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("social.events")

EXCHANGE = "social_events"

PUBLISHED_KEY = "post.published"
FAILED_KEY = "post.failed"

PREDECLARED_QUEUES: dict[str, list[str]] = {
    "notifications.social_events": [PUBLISHED_KEY, FAILED_KEY],
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
