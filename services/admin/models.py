"""
Admin/Ops service tables (Postgres schema `admin`, per spec):
  audit_log, accounts_directory, users_directory

audit_log is the spec's "consume all domain events into an audit_log table
for a searchable timeline per account (who did what, when)": APPEND-ONLY —
one row per domain event consumed off any exchange, deduped on event_id by
the idempotent consumer (processed_events). No route ever updates or deletes
a row; the only writer is consumer.handle_event. account_id/actor_user_id
are EXTRACTED copies of payload fields (indexed for the console's filters);
the full payload is kept verbatim in `payload` so nothing is lost when a
producer adds fields.

accounts_directory + users_directory are a tiny read model, NOT domain data
we own (the User service owns accounts/membership, Auth owns users). They
back the SuperAdmin console's "cross-account directory — search/browse all
accounts" without a cross-service call on every request: we learn rows from
the events we already consume for the audit log (account.created,
user.registered, member.joined, invoice.paid for the plan tier) — same
precedent as Notification's known_users/account_recipients. They also carry
this service's ONE piece of owned oversight state: the suspended flag.
Spec's Admin section publishes NO events, so suspension lives here and is
ENFORCED by revoking the target's active jti sessions in Redis (the
revocation switch the whole platform already honors at the Gateway) — the
User/Auth services are not called and keep owning their own data.

One placeholder-era wrinkle (see the Auth service): access tokens currently
carry account_id == user_id, so user_events rows are scoped by user_id in
the account_id column — the same convention Notification records.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from creditflow_common.db import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime read back from the DB to aware-UTC. Postgres
    returns aware values for DateTime(timezone=True); the SQLite test DB
    returns naive ones (already UTC — we only ever store UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class AuditLog(Base):
    """One row per consumed domain event (see module docstring). Append-only."""
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # The producer's event_id — also our processed_events dedup key, so a
    # redelivered event can never append twice.
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Which topic exchange the event arrived on (billing_events, ...).
    exchange: Mapped[str | None] = mapped_column(String(50), index=True, nullable=True)
    routing_key: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    # Tenant scope for the per-account timeline. Nullable: some events name
    # no account (and a malformed one may name nothing at all).
    account_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # "Who did it" — the payload's *_by_user_id / user_id field, when present.
    actor_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # Full event payload, JSON-serialized verbatim.
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True,
                                                 nullable=False, default=utcnow)


class AccountDirectory(Base):
    """Cross-account directory row, learned from account.* / invoice.paid
    events, plus this service's suspended flag."""
    __tablename__ = "accounts_directory"

    account_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # individual | team — from account.created.
    type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    plan_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # active | suspended — the ONE oversight field this service owns.
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="active")
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    suspend_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)


class UserDirectory(Base):
    """Platform-wide user row, learned from user.registered / member.joined,
    plus this service's suspended flag."""
    __tablename__ = "users_directory"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="active")
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    suspend_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)
