"""
Content service tables (Postgres schema `content`, per spec):
  content, content_versions

content is the live record the Scheduler and Social Publishing services will
read and update: one row per post, carrying the CURRENT title/body/image and
the server-enforced status machine (draft -> approved -> scheduled ->
published; see routes.TRANSITIONS for the legal moves and who may make them).

content_versions is the append-only edit history the spec's "edit (versioned)"
requirement asks for: one immutable row per version, INCLUDING version 1 at
creation, so the full lineage of any published post is reconstructable without
diffing. Rows are never UPDATEd or deleted while their content item exists.

Tags are stored comma-delimited with a leading/trailing comma (",a,b,") so a
single LIKE '%,tag,%' filters exactly on whole tags in both Postgres and the
SQLite test database; the API always speaks lists (see schemas/routes).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from creditflow_common.db import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ContentItem(Base):
    """One row per post. `version` mirrors the highest content_versions row —
    kept denormalized here so reads and event payloads never need a join."""
    __tablename__ = "content"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Audit attribution (spec §6) — the member who created the item (or, for
    # consumer-created drafts, who requested the generation).
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Link back to the AI generation this draft came from (API-supplied or set
    # by the ai.generation_completed consumer, which dedups on it).
    generation_job_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # ",a,b," normalized form; empty string when untagged (see module docstring).
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # draft | approved | scheduled | published — transitions enforced in routes.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Where the image is served from (our /content/{id}/image for uploads, or a
    # caller-supplied external URL) + the storage-relative ref for uploads.
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    image_asset_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)


class ContentVersion(Base):
    """Append-only snapshot of the content fields as of `version` — written at
    create (v1) and on every edit/image change, never on status transitions
    (status is workflow state, not content)."""
    __tablename__ = "content_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    content_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="")
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    image_asset_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    edited_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
