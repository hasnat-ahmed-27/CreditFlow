"""
store.py — the ONLY module that touches MongoDB.

The spec puts this service (alone in the stack) on a document store:
"Database Ownership: MongoDB — collection: scraped_documents" — raw pages
have wildly varied structure, so a flexible schema beats a relational one
here. Two collections:

  scrape_jobs        — the job lifecycle the API exposes: pending -> running
                       -> completed | failed. A recurring job (spec: "daily
                       competitor check") re-arms next_run_at after every run
                       and keeps producing documents; a one-off job sets
                       next_run_at to None on conclusion so the due-scan can
                       never pick it up again.
  scraped_documents  — one raw extracted document per successful run
                       (title/description/headings/text/links/html), the
                       source material downstream content generation reads.

TENANCY RULE: every document in both collections carries account_id, and
every read/write helper here requires it (the worker-internal helpers that
don't are named and documented as such) — routes can only ever hand out or
delete rows scoped to the caller's token.

Why everything funnels through this module: tests swap get_client() for a
mongomock client (see conftest) and the whole service — routes, tasks,
consumer — runs against in-memory Mongo with no infra.

Datetime discipline: BSON datetimes are millisecond-precision and pymongo
returns them NAIVE (mongomock returns them as stored). utcnow() therefore
truncates to whole milliseconds and as_utc() re-attaches UTC on read, so the
equality compare claim_run() does on next_run_at survives the
Celery-serialize -> Mongo -> read-back round trip on both drivers.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

from creditflow_common import config

DB_NAME = os.getenv("SCRAPER_MONGO_DB", "creditflow_scraper")

JOBS = "scrape_jobs"
DOCUMENTS = "scraped_documents"

# Single source of truth for the job vocabulary (schemas/routes import these).
JOB_STATUSES = ("pending", "running", "completed", "failed")

# Recurring cadences are plain UTC intervals — a scrape check needs "roughly
# daily", not the Scheduler's wall-clock-preserving timezone math.
RECURRENCE_DELTAS: dict[str, timedelta] = {
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
}
RECURRENCES = tuple(RECURRENCE_DELTAS)

_client: MongoClient | None = None


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Aware UTC truncated to whole milliseconds (BSON precision — see
    module docstring)."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def as_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime read back from Mongo to aware-UTC (pymongo
    returns naive UTC; mongomock returns what was stored)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_client() -> MongoClient:
    """Lazy client — created on first use, not at import time, so the Docker
    build-time smoke test needs no live Mongo. Tests monkeypatch THIS."""
    global _client
    if _client is None:
        _client = MongoClient(config.MONGO_URL)
    return _client


def get_db():
    return get_client()[DB_NAME]


def init() -> None:
    """Create the query-path indexes (idempotent) — called from the FastAPI
    lifespan, same role as the other services' init_db()."""
    db = get_db()
    db[JOBS].create_index([("account_id", ASCENDING), ("created_at", DESCENDING)])
    db[JOBS].create_index([("account_id", ASCENDING), ("status", ASCENDING)])
    db[JOBS].create_index([("next_run_at", ASCENDING)])
    db[JOBS].create_index([("request_event_id", ASCENDING)])
    db[DOCUMENTS].create_index([("account_id", ASCENDING), ("job_id", ASCENDING)])


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

def create_job(*, account_id: str, url: str, job_type: str = "page",
               recurrence: str | None = None, source: str = "api",
               request_event_id: str | None = None,
               created_by_user_id: str | None = None) -> dict:
    now = utcnow()
    job = {
        "_id": new_uuid(),
        "account_id": account_id,
        "url": url,
        "job_type": job_type,
        "recurrence": recurrence,
        "status": "pending",
        "source": source,                       # api | event (scrape.requested)
        "request_event_id": request_event_id,   # the consumed event, when source=event
        "created_by_user_id": created_by_user_id,
        "next_run_at": now,                     # due immediately; re-armed if recurring
        "last_run_at": None,
        "run_count": 0,
        "result_document_id": None,             # latest successful document
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    get_db()[JOBS].insert_one(job)
    return job


def ensure_job_for_event(event_id: str, **kwargs) -> dict:
    """Idempotent create for the scrape.requested consumer: if a crash landed
    the Mongo job but not the processed_events commit, the redelivered event
    finds the existing job instead of duplicating it."""
    existing = get_db()[JOBS].find_one({"request_event_id": event_id})
    if existing is not None:
        return existing
    return create_job(request_event_id=event_id, **kwargs)


def get_job(account_id: str, job_id: str) -> dict | None:
    return get_db()[JOBS].find_one({"_id": job_id, "account_id": account_id})


def list_jobs(account_id: str, *, status: str | None = None,
              job_type: str | None = None, limit: int = 20,
              offset: int = 0) -> tuple[list[dict], int]:
    where: dict = {"account_id": account_id}
    if status is not None:
        where["status"] = status
    if job_type is not None:
        where["job_type"] = job_type
    coll = get_db()[JOBS]
    total = coll.count_documents(where)
    rows = list(
        coll.find(where)
        .sort([("created_at", DESCENDING), ("_id", ASCENDING)])
        .skip(offset).limit(limit)
    )
    return rows, total


def delete_job(account_id: str, job_id: str) -> tuple[bool, int]:
    """Delete a job AND every document it produced (tenant-scoped). Returns
    (found, documents_deleted)."""
    db = get_db()
    job = db[JOBS].find_one_and_delete({"_id": job_id, "account_id": account_id})
    if job is None:
        return False, 0
    deleted = db[DOCUMENTS].delete_many(
        {"job_id": job_id, "account_id": account_id}).deleted_count
    return True, deleted


# --------------------------------------------------------------------------
# Run lifecycle (worker-internal — the task already holds a claimed job, so
# these two are keyed by job id alone)
# --------------------------------------------------------------------------

def due_jobs(now: datetime | None = None) -> list[dict]:
    """Jobs whose next occurrence has arrived — the beat scan's one query.
    Concluded one-off jobs have next_run_at None, so they can never match;
    a job already running is skipped (claim_run would refuse it anyway)."""
    return list(
        get_db()[JOBS]
        .find({"next_run_at": {"$ne": None, "$lte": now or utcnow()},
               "status": {"$ne": "running"}})
        .sort([("next_run_at", ASCENDING)])
    )


def claim_run(job_id: str, occurrence: datetime) -> dict | None:
    """Atomically flip a job to `running` — but only if it still exists, is
    not already running, AND its next_run_at still equals the occurrence the
    dispatcher saw. One find_one_and_update is the double-fire protection: a
    duplicate/stale task (beat overlap, redelivered dispatch, re-armed
    recurring job) matches nothing and gets None back."""
    return get_db()[JOBS].find_one_and_update(
        {"_id": job_id, "status": {"$ne": "running"}, "next_run_at": occurrence},
        {"$set": {"status": "running", "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )


def conclude_run(job_id: str, *, status: str, document_id: str | None = None,
                 error: str | None = None,
                 recurrence: str | None = None) -> dict | None:
    """Record a run's terminal outcome: recurring jobs re-arm next_run_at
    from now, one-offs clear it forever. Returns the updated job, or None if
    it was deleted mid-run."""
    now = utcnow()
    update: dict = {
        "status": status,
        "last_run_at": now,
        "error": error,
        "updated_at": now,
        "next_run_at": (now + RECURRENCE_DELTAS[recurrence]) if recurrence else None,
    }
    if document_id is not None:
        update["result_document_id"] = document_id
    return get_db()[JOBS].find_one_and_update(
        {"_id": job_id},
        {"$set": update, "$inc": {"run_count": 1}},
        return_document=ReturnDocument.AFTER,
    )


# --------------------------------------------------------------------------
# Scraped documents
# --------------------------------------------------------------------------

def store_document(*, account_id: str, job_id: str, url: str, job_type: str,
                   content: dict) -> dict:
    """Persist one raw extracted document (flexible schema — whatever the
    engine returned rides along)."""
    doc = {
        "_id": new_uuid(),
        "account_id": account_id,
        "job_id": job_id,
        "url": url,
        "job_type": job_type,
        "scraped_at": utcnow(),
        **content,
    }
    get_db()[DOCUMENTS].insert_one(doc)
    return doc


def get_document(account_id: str, document_id: str) -> dict | None:
    return get_db()[DOCUMENTS].find_one({"_id": document_id, "account_id": account_id})


def list_documents(account_id: str, job_id: str, *, limit: int = 20,
                   offset: int = 0) -> tuple[list[dict], int]:
    where = {"account_id": account_id, "job_id": job_id}
    coll = get_db()[DOCUMENTS]
    total = coll.count_documents(where)
    rows = list(
        coll.find(where)
        .sort([("scraped_at", DESCENDING), ("_id", ASCENDING)])
        .skip(offset).limit(limit)
    )
    return rows, total
