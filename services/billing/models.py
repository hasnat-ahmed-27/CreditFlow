"""
Billing service tables (Postgres schema `billing`, per spec):
  subscriptions, invoices, outbox_events, refunds
plus subscription_events — the raw-Stripe-webhook persistence table the spec
calls "the write side of the flow described in the section on reliability":
every webhook is stored here BEFORE any processing, and its unique
stripe_event_id makes duplicate webhook deliveries a no-op.

The reliability centerpiece is outbox_events (spec §7 Transactional Outbox):
a domain state change and the event describing it are inserted in the SAME
transaction, so they commit or roll back together. A poller (poller.py)
publishes unpublished rows to RabbitMQ and marks them — no event can be lost
even if the broker is down at commit time.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from creditflow_common.db import Base

PLANS = ("free", "pro", "team")
# Local subscription lifecycle. `past_due` is the dunning grace-period state:
# entered on invoice.payment_failed, left via invoice.paid (recovered) or
# grace expiry (downgraded to free by dunning.apply_due).
SUBSCRIPTION_STATUSES = ("active", "past_due", "canceled")


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(dt: datetime | None) -> datetime | None:
    """SQLite (used in tests) returns naive datetimes; treat them as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # One subscription row per tenant — spec §6: the plan is account-level.
    account_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    # "Create Stripe Customer ...; store stripe_customer_id" — this is where
    # webhooks are joined back to an account (Stripe only knows the customer).
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan: Mapped[str] = mapped_column(String(16), nullable=False, default="free")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Dunning: set on the FIRST payment_failed, cleared on recovery/downgrade.
    grace_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Unique so webhook redeliveries upsert instead of duplicating history.
    stripe_invoice_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Needed by the refund flow: a refund targets the payment intent.
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_due: Mapped[int] = mapped_column(Integer, nullable=False, default=0)     # cents
    amount_paid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)    # cents
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="usd")
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # paid | payment_failed | open | ...
    hosted_invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class SubscriptionEvent(Base):
    """Raw Stripe webhook persistence — written and COMMITTED before any
    processing, so a processing crash can never lose the event. processed_at
    is NULL until the state-change transaction commits; Stripe's redelivery
    (we 500 on processing failure) finds the row unprocessed and retries the
    processing step only."""
    __tablename__ = "subscription_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # Stripe event ids (evt_...) are globally unique → duplicate deliveries no-op.
    stripe_event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # full raw event JSON
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)  # e.g. "ignored (customer.created)"


class OutboxEvent(Base):
    """The Transactional Outbox (spec §7). Rows are inserted by outbox.stage()
    inside the SAME transaction as the domain change they describe; the poller
    publishes rows where published_at IS NULL and marks them. The row id is
    reused as the RabbitMQ event_id, so a crash between publish and mark (the
    at-least-once window) produces a duplicate consumers dedupe on."""
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    stripe_refund_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    stripe_payment_intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    invoice_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # our invoices.id, if known
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # cents
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="usd")
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Audit attribution (spec §6): the owner who requested it.
    requested_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
