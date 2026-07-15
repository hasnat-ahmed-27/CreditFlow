"""
Scraper endpoints — submit scrape jobs, read jobs/results, delete.

Authorization model (same as Scheduler/Content/AI/Social): identity comes
from the RS256 access token, verified with the shared public key.
Everything is scoped by the token's account_id claim — every store.py call
below passes it, and cross-account access answers 404, never 403, so ids
don't leak existence.

Role model: submitting and reading scrape jobs is any member's business —
scraping gathers source material for the content flow, it publishes nothing
outward. DELETE destroys the job AND every document it produced, so it
requires owner/admin (the same gate the other services put on destructive/
account-level actions).

SSRF gate: every submitted URL passes url_guard.validate_url (http/https
only, no credentials, no localhost/private/link-local/metadata targets) and
is rejected with 422 + the reason before a job is ever created. The consumer
applies the identical gate to broker-submitted URLs.

Execution is asynchronous (spec: "FastAPI (job endpoints) + worker process
for execution"): POST creates the job `pending` and dispatches
tasks.run_scrape to the Celery worker; the job's status walks
pending -> running -> completed | failed and the response of GET
/scrape-jobs/{id} carries the extracted document once there is one.
Recurring jobs are re-armed by the worker and re-fired by beat — see
tasks.py.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from creditflow_common import jwt_utils

import schemas
import store
import tasks
import url_guard

logger = logging.getLogger("scraper.routes")

router = APIRouter(tags=["scraper"])

# Roles that may delete a job and its scraped documents (spec roles:
# owner / admin / member) — destruction is account-level.
MANAGE_ROLES = ("owner", "admin")


def bearer_token(authorization: str = Header(default="")) -> str:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.split(" ", 1)[1]


def current_claims(token: str = Depends(bearer_token)) -> dict:
    """Verify the Bearer access token's RS256 signature + expiry."""
    try:
        claims = jwt_utils.verify_token(token)
    except jwt_utils.TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if claims.get("type") != "access":
        raise HTTPException(status_code=401, detail="Not an access token")
    return claims


def _iso(dt) -> str | None:
    normalized = store.as_utc(dt)
    return normalized.isoformat() if normalized else None


def job_dict(job: dict) -> dict:
    """API shape of a scrape job (raw Mongo doc in, JSON-safe dict out)."""
    return {
        "job_id": job["_id"],
        "account_id": job["account_id"],
        "url": job["url"],
        "job_type": job["job_type"],
        "recurrence": job.get("recurrence"),
        "status": job["status"],
        "source": job.get("source"),
        "run_count": job.get("run_count", 0),
        "result_document_id": job.get("result_document_id"),
        "error": job.get("error"),
        "request_event_id": job.get("request_event_id"),
        "created_by_user_id": job.get("created_by_user_id"),
        "next_run_at": _iso(job.get("next_run_at")),
        "last_run_at": _iso(job.get("last_run_at")),
        "created_at": _iso(job.get("created_at")),
        "updated_at": _iso(job.get("updated_at")),
    }


def document_dict(doc: dict, include_content: bool = True) -> dict:
    """API shape of a scraped document. Lists send the summary; single-get
    includes the extracted content and raw HTML (they can be large)."""
    out = {
        "document_id": doc["_id"],
        "account_id": doc["account_id"],
        "job_id": doc["job_id"],
        "url": doc.get("url"),
        "final_url": doc.get("final_url"),
        "job_type": doc.get("job_type"),
        "title": doc.get("title"),
        "description": doc.get("description"),
        "status_code": doc.get("status_code"),
        "scraped_at": _iso(doc.get("scraped_at")),
    }
    if include_content:
        out.update({
            "headings": doc.get("headings"),
            "text": doc.get("text"),
            "links": doc.get("links"),
            "html": doc.get("html"),
        })
    return out


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

@router.post("/scrape-jobs", status_code=201)
def create_scrape_job(
    body: schemas.ScrapeJobCreate,
    claims: dict = Depends(current_claims),
) -> dict:
    try:
        url_guard.validate_url(body.url)
    except url_guard.InvalidTargetURL as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    job = store.create_job(
        account_id=claims["account_id"],
        url=body.url,
        job_type=body.job_type,
        recurrence=body.recurrence,
        source="api",
        created_by_user_id=claims.get("sub"),
    )
    tasks.run_scrape.delay(job["_id"], store.as_utc(job["next_run_at"]).isoformat())
    # Re-read: in production the worker races ahead of nobody (the response
    # shows `pending`); under eager Celery the run already concluded and the
    # caller sees the terminal state immediately.
    return job_dict(store.get_job(claims["account_id"], job["_id"]) or job)


@router.get("/scrape-jobs")
def list_scrape_jobs(
    status: str | None = Query(default=None),
    job_type: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: dict = Depends(current_claims),
) -> dict:
    if status is not None and status not in store.JOB_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {store.JOB_STATUSES}")
    rows, total = store.list_jobs(claims["account_id"], status=status,
                                  job_type=job_type, limit=limit, offset=offset)
    return {"items": [job_dict(r) for r in rows],
            "total": total, "limit": limit, "offset": offset}


@router.get("/scrape-jobs/{job_id}")
def get_scrape_job(
    job_id: str,
    claims: dict = Depends(current_claims),
) -> dict:
    job = store.get_job(claims["account_id"], job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Scrape job not found")
    document = None
    if job.get("result_document_id"):
        document = store.get_document(claims["account_id"], job["result_document_id"])
    return {**job_dict(job),
            "document": document_dict(document) if document else None}


@router.get("/scrape-jobs/{job_id}/documents")
def list_job_documents(
    job_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: dict = Depends(current_claims),
) -> dict:
    """Every document a job has produced — a recurring job accumulates one
    per run; result_document_id on the job is only the latest."""
    if store.get_job(claims["account_id"], job_id) is None:
        raise HTTPException(status_code=404, detail="Scrape job not found")
    rows, total = store.list_documents(claims["account_id"], job_id,
                                       limit=limit, offset=offset)
    return {"items": [document_dict(r, include_content=False) for r in rows],
            "total": total, "limit": limit, "offset": offset}


@router.delete("/scrape-jobs/{job_id}")
def delete_scrape_job(
    job_id: str,
    claims: dict = Depends(current_claims),
) -> dict:
    # 404 before the role check (same order as the other services): a foreign
    # id must not learn it exists by drawing a 403.
    if store.get_job(claims["account_id"], job_id) is None:
        raise HTTPException(status_code=404, detail="Scrape job not found")
    if claims.get("role") not in MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="Requires a role with manage permission")
    _, documents_deleted = store.delete_job(claims["account_id"], job_id)
    return {"deleted": True, "job_id": job_id, "documents_deleted": documents_deleted}


# --------------------------------------------------------------------------
# Scraped documents (direct access — scrape.completed carries document_id)
# --------------------------------------------------------------------------

@router.get("/scraped-documents/{document_id}")
def get_scraped_document(
    document_id: str,
    claims: dict = Depends(current_claims),
) -> dict:
    doc = store.get_document(claims["account_id"], document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Scraped document not found")
    return document_dict(doc)
