"""
The generation worker — one asyncio task per accepted job. Owns the whole
stream lifecycle:

  OpenRouter stream --> Redis pub/sub chunk per token (store.publish_chunk)
                    --> Postgres persist (job final state + prompt_history)
                    --> RabbitMQ event (ai.generation_completed / _failed)

Ordering on finish: persist FIRST (durable truth), then the terminal chunk to
Redis (a client that sees "done" and immediately GETs the job reads the final
row), then the RabbitMQ event (never announce a write that didn't land).

Cancellation: the worker polls the job's Redis cancel flag between chunks and
stops consuming OpenRouter when it flips. A cancelled job still incurred real
upstream spend for the tokens already streamed, so it ALSO emits
ai.generation_completed — with `status: "cancelled"`, the partial text
persisted, and ESTIMATED token counts (OpenRouter only reports usage on the
final chunk we never reached; `usage_estimated: true` marks them). Usage
meters it like any completion; the future Content service can filter on the
status field.

Failures emit ai.generation_failed with the reason and never write history.

Blocking I/O (SQLAlchemy, pika) runs via asyncio.to_thread so a slow commit
or broker reconnect can't stall every in-flight stream on the event loop.
"""
from __future__ import annotations

import asyncio
import logging

import database
import events
import openrouter
import store
from models import GenerationJob, PromptRecord, utcnow
from usage_client import estimate_tokens

logger = logging.getLogger("ai.worker")

# Strong references so in-flight generation tasks are never garbage-collected.
_TASKS: set[asyncio.Task] = set()


def start(job: GenerationJob) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(
        run_generation(job.id, job.account_id, job.created_by_user_id,
                       job.model, job.prompt, job.max_tokens),
        name=f"generation-{job.id}",
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def run_generation(job_id: str, account_id: str, user_id: str | None,
                         model: str, prompt: str, max_tokens: int | None) -> None:
    await asyncio.to_thread(_set_status, job_id, "streaming")
    seq = 0
    parts: list[str] = []
    usage: dict | None = None
    cancelled = False
    try:
        stream = openrouter.stream_chat(model, prompt, max_tokens)
        async for kind, value in stream:
            if await store.cancel_requested(job_id):
                cancelled = True
                await stream.aclose()  # stop paying for tokens nobody wants
                break
            if kind == "token":
                parts.append(value)
                seq += 1
                await store.publish_chunk(job_id, {"seq": seq, "type": "token", "content": value})
            elif kind == "usage":
                usage = value
    except Exception as exc:  # noqa: BLE001 — any upstream failure ends the job
        reason = str(exc)[:500] or exc.__class__.__name__
        logger.warning("generation %s failed: %s", job_id, reason)
        await asyncio.to_thread(_finalize_failed, job_id, reason)
        await store.publish_chunk(job_id, {"seq": seq + 1, "type": "error", "reason": reason})
        await asyncio.to_thread(events.publish, "ai.generation_failed", {
            "account_id": account_id,
            "user_id": user_id,
            "job_id": job_id,
            "model": model,
            "reason": reason,
        })
        return

    response = "".join(parts)
    estimated = usage is None
    if estimated:
        # Cancelled mid-stream (or provider omitted usage): OpenRouter still
        # billed the tokens it streamed, so meter our best estimate.
        usage = {
            "input_tokens": estimate_tokens(prompt),
            "output_tokens": estimate_tokens(response),
            "total_tokens": estimate_tokens(prompt) + estimate_tokens(response),
            "cost_usd": None,
        }
    status = "cancelled" if cancelled else "completed"

    await asyncio.to_thread(_finalize_success, job_id, status, response, usage, estimated)
    await store.publish_chunk(job_id, {
        "seq": seq + 1,
        "type": "cancelled" if cancelled else "done",
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "usage_estimated": estimated,
    })
    await asyncio.to_thread(events.publish, "ai.generation_completed", {
        "account_id": account_id,
        "user_id": user_id,
        "job_id": job_id,
        "model": model,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": usage["cost_usd"],
        "status": status,
        "usage_estimated": estimated,
    })
    logger.info("generation %s %s: %d tokens (%s)", job_id, status,
                usage["total_tokens"], "estimated" if estimated else "reported")


# ---- Blocking DB helpers (run via asyncio.to_thread) ----------------------

def _set_status(job_id: str, status: str) -> None:
    db = database.SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is not None:
            job.status = status
            db.commit()
    finally:
        db.close()


def _finalize_failed(job_id: str, reason: str) -> None:
    db = database.SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.error_reason = reason
        job.completed_at = utcnow()
        db.commit()
    finally:
        db.close()


def _finalize_success(job_id: str, status: str, response: str,
                      usage: dict, estimated: bool) -> None:
    """Job final state + the prompt_history archive row, one transaction."""
    db = database.SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            return
        job.status = status
        job.response = response
        job.input_tokens = usage["input_tokens"]
        job.output_tokens = usage["output_tokens"]
        job.total_tokens = usage["total_tokens"]
        job.cost_usd = usage["cost_usd"]
        job.usage_estimated = estimated
        job.completed_at = utcnow()
        db.add(PromptRecord(
            job_id=job.id,
            account_id=job.account_id,
            created_by_user_id=job.created_by_user_id,
            model=job.model,
            prompt=job.prompt,
            response=response,
            total_tokens=usage["total_tokens"],
        ))
        db.commit()
    finally:
        db.close()
