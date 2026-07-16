"""
User/Tenant service tables (Postgres schema `user`, per spec):
  accounts, account_members, invites (+ processed_events for idempotency).

Multi-tenancy model:
  - An Account is the tenant. Individual signups and team workspaces are the
    SAME row shape, distinguished only by `type` — so credits, billing, and
    content can all scope by account_id without caring which kind it is.
  - account_members is the user<->account link with a per-account role.
    A user may appear in many accounts, with a different role in each; the
    account-scoped JWT (user_id + account_id + role) is minted from this table.
  - invites are stored as SHA-256 hashes (same rule as Auth's one-time tokens):
    the raw token only ever travels in the invite email — a DB leak must not
    let an attacker join arbitrary teams.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from creditflow_common.db import Base
# Importing registers the shared processed_events table on Base.metadata so
# create_all provisions it in this service's schema (see consumer.py).
from creditflow_common.idempotency import ProcessedEvent  # noqa: F401

ACCOUNT_TYPES = ("individual", "team")
ROLES = ("owner", "admin", "member")
INVITE_STATUSES = ("pending", "accepted", "revoked")


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(dt: datetime | None) -> datetime | None:
    """SQLite (used in tests) returns naive datetimes; treat them as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # individual | team
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    # Audit attribution (spec §6): who caused this tenant to exist.
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class AccountMember(Base):
    __tablename__ = "account_members"
    # One membership row per (account, user); the role lives here, NOT on the
    # user — the same user can be owner of their individual account and a
    # plain member of a team at the same time.
    __table_args__ = (UniqueConstraint("account_id", "user_id", name="uq_member_account_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)  # Auth-owned id, no FK across services
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # owner | admin | member
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # admin | member (never owner)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    invited_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
