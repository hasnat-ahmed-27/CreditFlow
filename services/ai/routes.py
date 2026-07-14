"""
AI generation endpoints.

Flow: POST /generations runs the synchronous quota check against the Usage
service (deny -> 429, Usage down -> 503 fail-closed), persists the job row,
then hands the stream to a background worker task and returns 202 with the
job id + stream URL. The client (or the Gateway, re-streaming) then GETs
/generations/{job_id}/stream — an SSE response fed from the job's Redis
pub/sub channel, so tokens render as they arrive (see store.py for why the
fan-out goes through Redis).

Authorization model (same as Usage/Credits): identity comes from the RS256
access token, verified with the shared public key. Jobs are scoped by the
token's account_id claim — spec §6: generations are account-level data, any
member may generate; cross-account access answers 404, never 403, so job ids
don't leak existence.

AI_EAGER_WORKER=1 (tests only) runs the worker inline before POST returns,
which makes the suite deterministic: every chunk is already in the Redis
replay buffer when the test opens the SSE stream.
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from creditflow_common import jwt_utils

import database
import openrouter
import schemas
import store
import usage_client
import worker
from models import GenerationJob

router = APIRouter(tags=["ai"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # tell nginx/gateway not to buffer the stream
}


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


def _owned_job(job_id: str, claims: dict, db: Session) -> GenerationJob:
    job = db.get(GenerationJob, job_id)
    if job is None or job.account_id != claims["account_id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _job_dict(job: GenerationJob) -> dict:
    return {
        "job_id": job.id,
        "account_id": job.account_id,
        "model": job.model,
        "status": job.status,
        "prompt": job.prompt,
        "response": job.response,
        "input_tokens": job.input_tokens,
        "output_tokens": job.output_tokens,
        "total_tokens": job.total_tokens,
        "cost_usd": float(job.cost_usd) if job.cost_usd is not None else None,
        "usage_estimated": job.usage_estimated,
        "error_reason": job.error_reason,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "stream_url": f"/generations/{job.id}/stream",
    }


@router.get("/models")
def list_models(claims: dict = Depends(current_claims)) -> dict:
    """The model choices a user can pick from (alias -> OpenRouter id)."""
    return {"models": [{"alias": alias, "model": model_id}
                       for alias, model_id in openrouter.MODELS.items()]}


@router.post("/generations", status_code=202)
async def create_generation(
    body: schemas.GenerationRequest,
    token: str = Depends(bearer_token),
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    model = openrouter.resolve_model(body.model)
    if model is None:
        raise HTTPException(status_code=422, detail=f"Unknown model {body.model!r}; see GET /models")

    # Synchronous quota gate BEFORE any job exists or any upstream spend.
    try:
        verdict = await usage_client.check_quota(token, usage_client.estimate_tokens(body.prompt))
    except usage_client.QuotaCheckError as exc:
        # Fail closed: no quota answer, no paid OpenRouter call.
        raise HTTPException(status_code=503, detail=f"Quota check unavailable: {exc}")
    if not verdict.get("allowed"):
        raise HTTPException(status_code=429, detail={
            "message": "Token quota exhausted for this account",
            "used_tokens": verdict.get("used_tokens"),
            "quota_tokens": verdict.get("quota_tokens"),
            "remaining_tokens": verdict.get("remaining_tokens"),
        })

    job = GenerationJob(
        account_id=claims["account_id"],
        created_by_user_id=claims.get("sub"),
        model=model,
        prompt=body.prompt,
        max_tokens=body.max_tokens,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    if os.getenv("AI_EAGER_WORKER", "0") == "1":
        await worker.run_generation(job.id, job.account_id, job.created_by_user_id,
                                    job.model, job.prompt, job.max_tokens)
    else:
        worker.start(job)

    return {"job_id": job.id, "status": "accepted", "model": model,
            "stream_url": f"/generations/{job.id}/stream"}


@router.get("/generations/{job_id}/stream")
async def stream_generation(
    job_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> StreamingResponse:
    """SSE stream of the job's tokens: replay of everything already produced,
    then live chunks, ending at the terminal event. Reconnect-safe — a fresh
    GET replays from the start of the Redis buffer."""
    _owned_job(job_id, claims, db)

    async def sse():
        async for message in store.stream_messages(job_id):
            yield f"event: {message['type']}\ndata: {json.dumps(message)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/generations/{job_id}/cancel", status_code=202)
async def cancel_generation(
    job_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Flag the job for cancellation; the worker stops at the next chunk
    boundary, persists the partial text, and closes the stream with a
    `cancelled` event (see worker.py for the metering semantics)."""
    job = _owned_job(job_id, claims, db)
    if job.status in ("completed", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Job already {job.status}")
    await store.request_cancel(job_id)
    return {"job_id": job_id, "status": "cancel_requested"}


@router.get("/generations/{job_id}")
def get_generation(
    job_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    return _job_dict(_owned_job(job_id, claims, db))
