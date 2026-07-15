"""
RabbitMQ consumer for the Scraper service.

Spec: "Accept scrape job requests via RabbitMQ (scrape.requested) with a
target URL/domain and job type." Consumes the durable queue
`scraper.scraper_events` on the `scraper_events` exchange — no service
publishes scrape.requested yet, so our own domain exchange is the agreed
rendezvous (see events.py); declaring the queue at startup means requests
queue up durably from the moment any future requester ships.

Expected payload: {account_id, url, job_type?, recurrence?,
requested_by_user_id?} — account_id is mandatory because every Mongo
document is tenant-scoped; a request without one belongs to nobody and is
dropped. URLs go through the same SSRF gate as the API (url_guard): a
broker message must not reach targets a JWT-authenticated caller could not.

Idempotency (spec §7): `already_processed(db, event_id)` + the
processed_events table (Postgres — see database.py) dedupe broker
redeliveries. Because the job row lives in Mongo, the two writes cannot
share one transaction like the all-Postgres services; the crash windows are
each covered instead:
  - crash after the Mongo insert, before the Postgres commit -> the
    redelivered event finds the job via store.ensure_job_for_event (keyed on
    request_event_id) instead of duplicating it;
  - crash after the commit, before the .delay dispatch -> the job sits
    `pending` with next_run_at in the past, and the next beat scan picks it
    up (tasks.scan_due_scrapes is the rescue path);
  - a duplicated dispatch -> store.claim_run's compare-and-set no-ops it.

Outcomes for bad requests: a payload with no account_id/url is logged and
dropped (recorded — malformed forever, like the other consumers); one with
an invalid/SSRF target is recorded AND answered with scrape.failed
(job_id null) so the requester learns why nothing will ever complete.

The consumer runs as a daemon thread (see main.py); the loop in `run()`
reconnects forever so a broker restart doesn't kill the service.
"""
from __future__ import annotations

import logging
import time

from creditflow_common import rabbitmq
from creditflow_common.idempotency import already_processed

import database
import events
import store
import tasks
import url_guard

logger = logging.getLogger("scraper.consumer")

EXCHANGE = "scraper_events"
QUEUE = "scraper.scraper_events"
ROUTING_KEYS = ["scrape.requested"]


def handle_event(routing_key: str, data: dict, event_id: str) -> None:
    db = database.SessionLocal()
    try:
        if already_processed(db, event_id):
            db.commit()
            logger.info("skipping already-processed event %s (%s)", event_id, routing_key)
            return

        account_id = data.get("account_id")
        url = data.get("url")
        if not account_id or not url:
            logger.warning("scrape.requested without account_id/url — dropping: %s", data)
            db.commit()  # still record the event_id: malformed forever
            return

        job_type = str(data.get("job_type") or "page")[:50]
        recurrence = data.get("recurrence")
        error = None
        if recurrence is not None and recurrence not in store.RECURRENCES:
            error = f"unknown recurrence {recurrence!r} (expected one of {store.RECURRENCES})"
        else:
            try:
                url_guard.validate_url(url)
            except url_guard.InvalidTargetURL as exc:
                error = str(exc)
        if error:
            db.commit()  # recorded: this request can never become valid
            events.publish(events.FAILED_KEY, {
                "job_id": None,
                "account_id": account_id,
                "url": url,
                "job_type": job_type,
                "recurrence": recurrence,
                "request_event_id": event_id,
                "error": error,
            })
            logger.warning("rejected scrape.requested %s: %s", event_id, error)
            return

        job = store.ensure_job_for_event(
            event_id,
            account_id=account_id,
            url=url,
            job_type=job_type,
            recurrence=recurrence,
            source="event",
            created_by_user_id=data.get("requested_by_user_id"),
        )
        db.commit()  # processed_events lands before the dispatch
        # A redelivered event may find the job already concluded (one-off:
        # next_run_at None) — then there is nothing left to dispatch.
        if job.get("next_run_at") is not None:
            tasks.run_scrape.delay(job["_id"], store.as_utc(job["next_run_at"]).isoformat())
        logger.info("scrape.requested %s -> job %s (%s)", event_id, job["_id"], url)
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
