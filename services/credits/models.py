"""
Credits/Marketplace service tables (Postgres schema `credits`, per spec):
  credits_ledger, marketplace_listings, processed_events

Why an APPEND-ONLY ledger instead of a mutable balance column:
  - Concurrency: two writers UPDATE-ing one balance row must serialize on a
    row lock and one can silently overwrite the other (lost update). Two
    INSERTs never conflict — the balance is SUM(amount), computed under
    whatever isolation the reader needs.
  - Auditability: every credit movement is a permanent row with who/why/when
    attached. A mutable column only knows the current number; "why is my
    balance 37?" becomes unanswerable the moment the UPDATE commits.
  - Idempotency/recovery: a redelivered event is answerable by looking for
    the row it would have written (stripe_ref below). With UPDATE
    balance = balance + N there is nothing to look for — replays corrupt.
  - Reversibility: a refund claw-back is just another row pointing at the
    grant it reverses (related_entry_id) — history is never rewritten.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from creditflow_common.db import Base
# Importing registers the shared processed_events table on Base.metadata so
# create_all provisions it in this service's schema (see consumer.py).
from creditflow_common.idempotency import ProcessedEvent  # noqa: F401

# amount > 0 rows (credits in) vs amount < 0 rows (credits out).
ENTRY_TYPES = (
    "purchase_grant",       # invoice.paid consumed -> plan credits in
    "refund_clawback",      # refund.issued consumed -> grant reversed
    "usage_debit",          # POST /credits/consume (AI usage spend)
    "marketplace_debit",    # seller side of a marketplace purchase
    "marketplace_credit",   # buyer side of a marketplace purchase
)
LISTING_STATUSES = ("open", "sold", "canceled")


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LedgerEntry(Base):
    """One immutable row per credit movement. Rows are only ever INSERTed —
    never UPDATEd or DELETEd. An account's balance is SUM(amount)."""
    __tablename__ = "credits_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Signed credits: positive = credited, negative = debited. Never zero.
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Business-key dedup on top of event_id idempotency: stripe_invoice_id on
    # grants, stripe_refund_id on claw-backs. If Billing re-emits the same
    # invoice under a FRESH event_id, this is what still catches it.
    stripe_ref: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    listing_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # The other account in a marketplace transfer (buyer on the seller's row
    # and vice versa) — keeps each row self-explanatory for the history view.
    counterparty_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # refund_clawback -> the purchase_grant row it reverses. Also the guard
    # that a grant can only be clawed back once.
    related_entry_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # Real-money side of the movement, in cents (invoice amount_paid on
    # grants, refund amount on claw-backs, listing price on transfers).
    money_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Audit attribution (spec §6) — NULL for consumer-driven rows (no user acted).
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MarketplaceListing(Base):
    """An account offering surplus credits for sale. Credits stay in the
    seller's ledger until purchase (no escrow row) — the purchase transaction
    re-checks the seller's balance and claims the listing with an optimistic
    status='open' UPDATE, so a listing can sell at most once."""
    __tablename__ = "marketplace_listings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    seller_account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    credits_amount: Mapped[int] = mapped_column(Integer, nullable=False)  # > 0
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)     # > 0
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    buyer_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
