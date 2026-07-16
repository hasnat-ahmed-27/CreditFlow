"""
RabbitMQ consumer for the Content service.

Consumes the durable queue `content.usage_events` on the `usage_events`
exchange — the queue the AI service PRE-DECLARED for us (its events.py binds
ai.generation_completed there), so every completion emitted since the AI
service first started is already waiting when this consumer comes up. We
re-declare on connect (idempotent), same contract as every other service.

Spec: "Consume ai.generation_completed to create a draft content record when
generation was for a 'post' type." A draft is created only when ALL hold:

  - status == "completed"      (cancelled generations are metered by Usage
                                but produced partial text nobody asked to
                                keep; failed ones emit a different key)
  - generation_type == "post"  (the spec's 'post' type marker)
  - response (the generated text) is present and non-empty

FORWARD CONTRACT: the AI service's current payload — {account_id, user_id,
job_id, model, input_tokens, output_tokens, total_tokens, cost_usd, status,
usage_estimated} — carries neither generation_type nor response yet; the AI
service adds both when the Content Studio flow marks a generation as a post.
Until then every event is recorded in processed_events and skipped, which is
correct: no generation is post-typed yet, and nothing is lost (the draft for
a post-typed generation is created from its OWN event, not retroactively).

Idempotency (spec §7 — consumers must survive at-least-once redelivery),
two independent layers, same as Usage/Credits:
  1. `already_processed(db, event_id)` + the processed_events table dedupe
     broker redeliveries: the event row and the content row commit in the
     SAME transaction, so either both exist or neither does.
  2. A business-key guard on the content itself (generation_job_id) — covers
     the AI service re-emitting the same generation under a FRESH event_id.

content.created is published only AFTER the commit (never announce a write
that didn't land), reusing routes.event_payload so consumer-created drafts
look identical on the wire to API-created ones.

The consumer runs as a daemon thread (see main.py); the loop in `run()`
reconnects forever so a broker restart doesn't kill the service. A raising
handler makes the shared consumer retry (bounded) and then dead-letter.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from creditflow_common import rabbitmq
from creditflow_common.idempotency import already_processed

import database
import events
import routes
from models import ContentItem, ContentVersion

logger = logging.getLogger("content.consumer")

EXCHANGE = "usage_events"         # the AI service publishes here
QUEUE = "content.usage_events"    # pre-declared for us by the AI service
ROUTING_KEYS = ["ai.generation_completed"]

TITLE_MAX = 120


def handle_event(routing_key: str, data: dict, event_id: str) -> None:
    db = database.SessionLocal()
    try:
        if already_processed(db, event_id):
            db.commit()
            logger.info("skipping already-processed event %s (%s)", event_id, routing_key)
            return
        created_payload = None
        if routing_key == "ai.generation_completed":
            created_payload = _create_draft(db, data)
        db.commit()  # processed_events row + content rows land atomically
        if created_payload is not None:
            events.publish("content.created", created_payload)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _create_draft(db, data: dict) -> dict | None:
    """Create the draft + v1 snapshot; returns the content.created payload,
    or None when the event doesn't qualify (see module docstring)."""
    account_id = data.get("account_id")
    job_id = data.get("job_id")
    if not account_id or not job_id:
        logger.warning("ai.generation_completed without account_id/job_id — dropping: %s", data)
        return None
    if data.get("status") != "completed":
        logger.info("job %s status %r — not draft material, skipping", job_id, data.get("status"))
        return None
    if data.get("generation_type") != "post":
        logger.info("job %s is not a 'post' generation — skipping", job_id)
        return None
    text = (data.get("response") or "").strip()
    if not text:
        logger.warning("post-typed job %s carried no response text — skipping", job_id)
        return None

    # Business-key guard: one draft per generation job, ever.
    existing = db.scalar(select(ContentItem).where(ContentItem.generation_job_id == job_id))
    if existing is not None:
        logger.info("job %s already has content %s — skipping", job_id, existing.id)
        return None

    item = ContentItem(
        account_id=account_id,
        created_by_user_id=data.get("user_id"),
        generation_job_id=job_id,
        title=_derive_title(text),
        body=text,
    )
    db.add(item)
    db.flush()
    db.add(ContentVersion(
        content_id=item.id,
        account_id=item.account_id,
        version=1,
        title=item.title,
        body=item.body,
        tags="",
        edited_by_user_id=data.get("user_id"),
    ))
    logger.info("created draft %s from generation job %s (%s)", item.id, job_id, account_id)
    return routes.event_payload(item, created_by_user_id=data.get("user_id"))


def _derive_title(text: str) -> str:
    """First non-empty line, truncated — a human renames it in the Studio."""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line[:TITLE_MAX] or "AI generated draft"


def run() -> None:
    """Blocking consume loop with reconnect — target for the daemon thread."""
    while True:
        try:
            rabbitmq.consume(
                exchange=EXCHANGE,
                queue=QUEUE,
                routing_keys=ROUTING_KEYS,
                handler=handle_event,
            )
        except Exception:  # noqa: BLE001 — broker hiccup: log, back off, reconnect
            logger.exception("consumer connection lost — retrying in 5s")
            time.sleep(5)
