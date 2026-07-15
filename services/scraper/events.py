"""
Domain-event publishing for the Scraper service — topic exchange
`scraper_events` (one of the spec's four named domain exchanges), durable +
publisher confirms + persistent messages via
creditflow_common.rabbitmq.Publisher.

ROUTING-KEY CONTRACT (spec verbatim: "Emit scrape.completed with a reference
to the stored document(s); emit scrape.failed on error"):
  - `scrape.completed` — a run stored a document. Payload carries the
    MongoDB reference downstream services need: document_id (in the
    scraped_documents collection), plus job_id, account_id, url, job_type,
    recurrence, run_count, and request_event_id (the consumed
    scrape.requested event, when the job came in over the broker).
  - `scrape.failed` — a run concluded in failure. Same identifiers plus
    `error` (the reason). A malformed/SSRF-rejected scrape.requested also
    answers with this key (job_id null — no job was ever created).

The same exchange carries the key we CONSUME, `scrape.requested` (see
consumer.py): no service is spec'd to publish it yet, so scraper_events is
the agreed rendezvous — any future requester (Content research flow, the
Gateway, an admin tool) publishes scrape.requested here, exactly like the AI
service publishes into `usage_events` for Usage.

Pre-declared durable queue (same contract as the other services): the shared
Publisher sends with mandatory=True, so every routing key needs at least one
bound queue or the publish bounces as unroutable.
  - `content.scraper_events` — scraped data exists to feed content
    generation (spec: "feeding raw data for content generation"; acceptance:
    "stores data usable by the content flow"). Declaring the queue now means
    completion events accumulate durably from day one, the same forward
    contract the AI service made for Content with `content.usage_events`.
    The Admin service (13) binds `#` on all exchanges itself for the audit
    log, so it needs no pre-declared queue.

Posture: best-effort like the other services — a broker outage is logged but
never fails the caller; the durable truth (the job + document in Mongo) is
already written by the time we publish.
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("scraper.events")

EXCHANGE = "scraper_events"

COMPLETED_KEY = "scrape.completed"
FAILED_KEY = "scrape.failed"

PREDECLARED_QUEUES: dict[str, list[str]] = {
    "content.scraper_events": [COMPLETED_KEY, FAILED_KEY],
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
