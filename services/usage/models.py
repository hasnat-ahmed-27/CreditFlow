"""
Usage/Metering service tables (Postgres schema `usage`, per spec):
  usage_ledger, processed_events

Why the Redis-counter + Postgres-ledger SPLIT (spec: "Redis (live counters) +
PostgreSQL (usage_ledger, append-only)"):
  - The quota pre-check sits on the hot path of EVERY AI generation request —
    it must be a single O(1) Redis read, not a SUM over a growing table.
  - The ledger is the durable truth: one immutable row per generation with
    model/tokens/cost attached, so "why is my usage 83k tokens?" is always
    answerable and the Admin dashboard can aggregate by model. Redis is a
    cache of SUM(total_tokens) for the current period and may be lost or
    drift — it is always rebuilt FROM the ledger (see quota.py), never the
    other way around.
  - Append-only for the same reasons as the credits ledger: concurrent
    INSERTs never conflict, replays are answerable by looking for the row
    they would have written (job_id below), and history is never rewritten.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from creditflow_common.db import Base
# Importing registers the shared processed_events table on Base.metadata so
# create_all provisions it in this service's schema (see consumer.py).
from creditflow_common.idempotency import ProcessedEvent  # noqa: F401


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UsageEntry(Base):
    """One immutable row per completed AI generation. Rows are only ever
    INSERTed — never UPDATEd or DELETEd. An account's usage for a period is
    SUM(total_tokens) over its rows."""
    __tablename__ = "usage_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Business-key dedup on top of event_id idempotency: one ledger row per
    # AI generation job, ever. If the AI service re-emits the same job under
    # a FRESH event_id, this is what still catches it.
    job_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    # Provider cost in USD (OpenRouter bills fractions of a cent, so cents
    # would truncate). Informational — the credit debit is Credits' job.
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    # Calendar month (UTC, "YYYY-MM") the usage counts against — matches the
    # Redis counter key so period sums need no date arithmetic.
    period: Mapped[str] = mapped_column(String(7), index=True, nullable=False)
    # Audit attribution (spec §6) — the member who ran the generation.
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
