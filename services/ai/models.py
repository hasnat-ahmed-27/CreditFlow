"""
AI Generation service tables (Postgres schema `ai`, per spec):
  generation_jobs, prompt_history

generation_jobs is the job's live state machine (pending -> streaming ->
completed | failed | cancelled) plus the final token/cost accounting once
streaming ends. It is the row the SSE stream, the cancel endpoint, and the
status endpoint all key off (the job id IS the Redis channel key).

prompt_history is the durable prompt/response archive the spec asks for
("persist full prompt + final response + token counts"): one immutable row
per generation that produced text (completed or cancelled-with-partial).
Failed jobs keep their error on generation_jobs but write no history row —
there is no response to archive.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from creditflow_common.db import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GenerationJob(Base):
    """One row per generation request. The primary key doubles as the job_id
    in the SSE stream URL, the Redis pub/sub channel, the cancel flag, and
    the ai.generation_completed payload (Usage's business dedup key)."""
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Audit attribution (spec §6) — the member who requested the generation.
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Full OpenRouter model id (aliases are resolved before the row is written).
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # pending | streaming | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Final (possibly partial, if cancelled) response text.
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Provider cost in USD, as reported by OpenRouter's usage accounting.
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    # True when token counts are our estimate (cancelled mid-stream, or the
    # provider omitted usage) rather than provider-reported numbers.
    usage_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PromptRecord(Base):
    """Append-only prompt/response archive — never UPDATEd, one row per
    generation that produced text."""
    __tablename__ = "prompt_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
