"""
Celery tasks — the scrape execution loop.

scan_due_scrapes (beat, every SCRAPER_SCAN_INTERVAL seconds): one cheap Mongo
query for jobs whose next_run_at has arrived, then one run_scrape task PER
due job — so a slow scrape never blocks the scan, and each run carries its
own double-fire protection. Recurring jobs (the spec's "daily competitor
check") come due here every time conclude_run re-arms them; it is ALSO the
rescue path for a one-off job whose dispatch was lost (crash between the
consumer's commit and .delay — the job sits pending with next_run_at in the
past until the next scan picks it up).

run_scrape(job_id, occurrence_iso): claims the job with store.claim_run —
ONE atomic find_one_and_update that requires the job to exist, not be
running, and still carry the exact next_run_at the dispatcher saw. That
single compare-and-set is the double-fire protection (Mongo gives us the
atomicity the Scheduler needed a Redis lock + DB re-check to build): a beat
overlap, a duplicated dispatch, or a straggler task for an occurrence a
recurring job already re-armed past all match nothing and no-op. Then:
engine scrape -> store the document -> conclude the job -> emit the event.

Ordering follows the repo rule — write FIRST, then publish (never announce
a write that didn't land): the scraped_documents insert and the job's
terminal update are in Mongo before scrape.completed / scrape.failed goes
out. Any engine exception concludes the run as failed (a scrape has no
transient/permanent split worth retrying blind — the next occurrence of a
recurring job IS the retry); a job deleted mid-run concludes nothing and
announces nothing.

Tasks return a short status string ("completed" / "failed" / "stale" / ...)
— it lands in the Celery result backend, which makes production debugging
via `celery result` and test assertions equally direct.
"""
from __future__ import annotations

import logging
from datetime import datetime

import events
import scraper_engine
import store

from celery_app import celery

logger = logging.getLogger("scraper.tasks")


def _event_payload(job: dict, **extra) -> dict:
    return {
        "job_id": job["_id"],
        "account_id": job["account_id"],
        "url": job["url"],
        "job_type": job["job_type"],
        "recurrence": job.get("recurrence"),
        "run_count": job.get("run_count", 0),
        "request_event_id": job.get("request_event_id"),
        **extra,
    }


@celery.task(name="tasks.scan_due_scrapes")
def scan_due_scrapes() -> int:
    """Beat entrypoint: dispatch one run_scrape per due job. Returns how many
    were dispatched (visible in the result backend)."""
    due = store.due_jobs()
    for job in due:
        # The occurrence timestamp pins the task to THIS firing: after a
        # recurring re-arm, a straggler task for the old occurrence is stale
        # by definition (see store.claim_run).
        run_scrape.delay(job["_id"], store.as_utc(job["next_run_at"]).isoformat())
    if due:
        logger.info("dispatched %d due scrape job(s)", len(due))
    return len(due)


@celery.task(name="tasks.run_scrape")
def run_scrape(job_id: str, occurrence_iso: str) -> str:
    occurrence = datetime.fromisoformat(occurrence_iso)

    job = store.claim_run(job_id, occurrence)
    if job is None:
        logger.info("scrape job %s no longer due at %s — skipping", job_id, occurrence_iso)
        return "stale"

    try:
        result = scraper_engine.scrape(job["url"], job["job_type"])
    except Exception as exc:  # noqa: BLE001 — any engine failure concludes the run
        reason = str(exc)[:500] or exc.__class__.__name__
        logger.warning("scrape job %s failed: %s", job_id, reason)
        job = store.conclude_run(job_id, status="failed", error=reason,
                                 recurrence=job.get("recurrence"))
        if job is None:
            return "deleted"  # job removed mid-run: nothing to announce
        events.publish(events.FAILED_KEY, _event_payload(job, error=reason))
        return "failed"

    document = store.store_document(
        account_id=job["account_id"],
        job_id=job_id,
        url=job["url"],
        job_type=job["job_type"],
        content=result,
    )
    job = store.conclude_run(job_id, status="completed", document_id=document["_id"],
                             recurrence=job.get("recurrence"))
    if job is None:
        return "deleted"
    events.publish(events.COMPLETED_KEY, _event_payload(job, document_id=document["_id"]))
    logger.info("scrape job %s completed -> document %s (run %d)",
                job_id, document["_id"], job["run_count"])
    return "completed"
