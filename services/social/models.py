"""
Social Publishing service tables (Postgres schema `social`, per spec):
  social_connections, publish_jobs, post_media

social_connections is the spec's "store encrypted access/refresh tokens per
account" table: one row per (account, provider) LinkedIn link, carrying the
member URN posts are authored under and the Fernet-encrypted OAuth tokens
(see crypto.py — token values never leave this table unencrypted and are
never logged or returned by the API). Disconnect is soft (status flips to
`disconnected`, tokens are CLEARED) so the connection history survives while
the secrets do not; reconnecting re-uses the row.

publish_jobs is the audit trail of every publish attempt — manual (the
POST /publish route) or scheduled (the content.scheduled consumer) — with
the exact text that went out, the LinkedIn post id/URL on success, and the
failure reason otherwise. Terminal statuses only (`published` | `failed`):
a job row is written in the same transaction that concludes the attempt, so
there is no `pending` limbo; transient failures in the consumer commit
nothing and let the broker redeliver.

post_media is the spec's record of the image-upload bonus flow: one row per
image pushed through LinkedIn's register-upload -> PUT binary -> asset URN
pipeline, linking the source image to the LinkedIn asset referenced by the
UGC post.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
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


class SocialConnection(Base):
    """One LinkedIn link per account. Tokens at rest are Fernet ciphertext."""
    __tablename__ = "social_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="linkedin")
    # urn:li:person:{sub} — the author of every post published through this link.
    member_urn: Mapped[str | None] = mapped_column(String(120), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Fernet ciphertext (crypto.py). Cleared (None) on disconnect.
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # connected | disconnected — soft disconnect keeps the history row.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="connected", index=True)
    # Audit attribution (spec §6) — the member who ran the OAuth flow.
    connected_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)


class PublishJob(Base):
    """One row per concluded publish attempt (see module docstring)."""
    __tablename__ = "publish_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    content_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Set for consumer-driven jobs: the scheduler row and fire event behind them.
    schedule_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # manual (POST /publish) | scheduled (content.scheduled consumer).
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # published | failed — terminal only (see module docstring).
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Snapshot of the commentary that went (or would have gone) out.
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # content = authoritative Content-service record | event = the fire
    # payload's title/image_url mirrors (see consumer.py for when each applies).
    text_source: Mapped[str] = mapped_column(String(16), nullable=False, default="content")
    image_included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    linkedin_post_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    linkedin_post_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)


class PostMedia(Base):
    """One row per image that completed LinkedIn's upload pipeline."""
    __tablename__ = "post_media"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    publish_job_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Where the bytes came from (Content-relative path or absolute URL).
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    linkedin_asset_urn: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
