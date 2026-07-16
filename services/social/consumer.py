"""
RabbitMQ consumer for the Social Publishing service.

Consumes the durable queue `social.scheduler_events` on the `scheduler_events`
exchange — the queue the Scheduler service PRE-DECLARED for us (its events.py
binds `content.scheduled` there), so every fire event since the Scheduler
shipped is already waiting when this consumer comes up. We re-declare on
connect (idempotent), same contract as every other service.

Spec: "Consume content.scheduled; if the content item has an attached image,
run the image upload flow before creating the post; otherwise create a
text-only post." The fire payload (see the Scheduler's events.py) carries
{schedule_id, account_id, content_id, publish_at, fired_at, timezone,
recurrence, fire_count, title, image_url, created_by_user_id}.

WHERE THE POST TEXT COMES FROM — the Scheduler documents content_id as "the
pointer Social follows to fetch the authoritative post body", but Content's
API tenant-scopes by JWT and there is no service-auth story yet (the same
KNOWN GAP the Scheduler recorded for its side of this wiring):
  - If SOCIAL_CONTENT_TOKEN is set (dev stopgap: a token whose account
    matches the content — fine for the single-account demo flow, NOT a
    multi-tenant answer), we fetch GET /content/{id} and post the
    authoritative body; a 404 there means the content is gone and the job
    fails honestly. text_source="content".
  - Otherwise (or on 401/403) we degrade to the fire payload's own
    title/image_url mirrors — current as of fire time, just not the full
    body. text_source="event". When service auth lands, setting a real
    service token upgrades this path with no code change.
Uploaded images served from Content's authed /content/{id}/image route
follow the same rule (the bearer is forwarded to content_client.get_image);
absolute URLs (AI-generated images) need no auth and work today.

Idempotency (spec §7 — the reason a redelivered fire event never
double-posts): `already_processed(db, event_id)` + the processed_events
table dedupe broker redeliveries; the event row and the publish_jobs/
post_media rows commit in the SAME transaction, so either the event is
recorded as done WITH its outcome or neither exists. TRANSIENT failures
(LinkedIn/network 429/5xx) raise before any commit — the shared consumer
retries with backoff and dead-letters after MAX_RETRIES (spec: "Retry with
backoff on transient LinkedIn API failures; route to dead-letter queue
after max attempts") — while PERMANENT failures conclude the job as failed
and emit post.failed. Events are emitted only AFTER the commit
(commit-first rule).

The consumer runs as a daemon thread (see main.py); the loop in `run()`
reconnects forever so a broker restart doesn't kill the service.
"""
from __future__ import annotations

import logging
import os
import time

from creditflow_common import rabbitmq
from creditflow_common.idempotency import already_processed

import content_client
import database
import publishing

logger = logging.getLogger("social.consumer")

EXCHANGE = "scheduler_events"          # the Scheduler service publishes here
QUEUE = "social.scheduler_events"      # pre-declared for us by the Scheduler
ROUTING_KEYS = ["content.scheduled"]


def _service_token() -> str:
    """Dev-stopgap bearer for Content fetches (see module docstring)."""
    return os.getenv("SOCIAL_CONTENT_TOKEN", "")


def _resolve_content(data: dict) -> tuple[str, str | None, str]:
    """(text, image_url, text_source) — authoritative Content record when a
    service token is configured and accepted, else the fire payload mirrors.
    Transient Content errors and permanent non-auth errors (e.g. 404: the
    content was deleted after firing) propagate to handle_event."""
    token = _service_token()
    if token:
        try:
            item = content_client.get_content(data["content_id"], token)
            return (item.get("body") or item.get("title") or "",
                    item.get("image_url"), "content")
        except content_client.ContentTransientError:
            raise
        except content_client.ContentClientError as exc:
            if exc.status_code not in (401, 403):
                raise
            logger.warning("SOCIAL_CONTENT_TOKEN rejected by Content (%s) — "
                           "falling back to fire-payload mirrors", exc.status_code)
    return data.get("title") or "", data.get("image_url"), "event"


def handle_event(routing_key: str, data: dict, event_id: str) -> None:
    db = database.SessionLocal()
    try:
        if already_processed(db, event_id):
            db.commit()
            logger.info("skipping already-processed event %s (%s)", event_id, routing_key)
            return

        account_id = data.get("account_id")
        content_id = data.get("content_id")
        if not account_id or not content_id:
            logger.warning("content.scheduled without account_id/content_id — dropping: %s", data)
            db.commit()  # still record the event_id: malformed forever
            return
        schedule_id = data.get("schedule_id")

        connection = publishing.active_connection(db, account_id)
        if connection is None:
            job = publishing.failed_job(
                account_id=account_id, content_id=content_id,
                reason="no connected LinkedIn account for this account",
                source="scheduled", schedule_id=schedule_id, event_id=event_id,
                text=data.get("title") or "", text_source="event",
                image_included=bool(data.get("image_url")),
            )
            db.add(job)
            db.commit()
            publishing.emit_result(job)
            return

        try:
            text, image_url, text_source = _resolve_content(data)
        except content_client.ContentTransientError:
            raise  # broker retry — nothing committed
        except content_client.ContentClientError as exc:
            job = publishing.failed_job(
                account_id=account_id, content_id=content_id,
                reason=f"content fetch failed: {exc}",
                source="scheduled", schedule_id=schedule_id, event_id=event_id,
                connection_id=connection.id,
            )
            db.add(job)
            db.commit()
            publishing.emit_result(job)
            return

        job = publishing.run_publish(
            db,
            connection=connection,
            account_id=account_id,
            content_id=content_id,
            text=text,
            image_url=image_url,
            image_bearer=_service_token() or None,
            source="scheduled",
            schedule_id=schedule_id,
            event_id=event_id,
            text_source=text_source,
            fail_on_transient=False,  # transient -> raise -> broker retry/DLQ
        )
        db.commit()  # processed_events + publish_jobs/post_media land atomically
        publishing.emit_result(job)
        logger.info("schedule %s content %s -> %s", schedule_id, content_id, job.status)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
