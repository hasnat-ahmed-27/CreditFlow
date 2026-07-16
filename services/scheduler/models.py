"""
Scheduler service tables (Postgres schema `scheduler`, per spec):
  scheduled_posts, content_refs

scheduled_posts is the spec's table: one row per calendar entry linking an
existing content item (by id — content is never duplicated here) to a future
publish_at. Times are stored in UTC (spec: "Store schedule times in UTC");
the IANA `timezone` string is kept alongside so recurring schedules re-arm in
the user's wall-clock time (a weekly 9am post stays 9am across DST) and so
the frontend can label the calendar without guessing.

Status machine (enforced in routes/tasks): pending -> fired | cancelled.
A one-off schedule moves to `fired` the moment its event is emitted. A
RECURRING schedule (the spec's "important" bonus — repeat on a cadence, e.g.
weekly) never becomes `fired`: each firing bumps fire_count/last_fired_at and
re-arms publish_at to the next occurrence, staying `pending` until cancelled.
`fired` and `cancelled` are terminal.

content_refs is a read model, not owned data: it mirrors what the Content
service announces on the content_events exchange (see consumer.py), so
schedule creation can validate "this content exists, belongs to your account,
and is approved" without a synchronous cross-service call, and content
deletion can cancel the schedules pointing at it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
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


class ScheduledPost(Base):
    """One calendar entry. `publish_at` is always the NEXT occurrence (UTC);
    for recurring schedules it advances on every firing."""
    __tablename__ = "scheduled_posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    content_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Audit attribution (spec §6) — the member who placed it on the calendar.
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Next fire time, UTC.
    publish_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    # IANA zone the user scheduled in — recurrence math happens here.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    # None = one-off; daily | weekly | monthly = the bonus cadence.
    recurrence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # pending | fired | cancelled — transitions enforced in routes/tasks.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    fire_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)


class ContentRef(Base):
    """Mirror of a Content-service item, maintained by consumer.py from
    content.* events (last-write-wins). Only what scheduling needs: identity,
    tenancy, workflow status, and the display fields the fire event carries."""
    __tablename__ = "content_refs"

    content_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Content-service lifecycle status (draft/approved/scheduled/published) —
    # only approved/scheduled content may be placed on the calendar.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)
