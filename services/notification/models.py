"""
Notification service tables (Postgres schema `notification`, per spec):
  notification_log, known_users, account_recipients

notification_log is the spec's "log every notification sent (type, recipient,
status) for auditing" table: one row per CONCLUDED send attempt — the exact
recipient/subject that went out, which provider carried it (resend | mailgun)
and its message id on success, or the failure reason otherwise. Terminal
statuses only (`sent` | `failed`), same rule as Social's publish_jobs:
transient provider failures commit nothing and let the broker redeliver, so
there is no `pending` limbo and the log never counts an email twice.

known_users + account_recipients are a tiny read model, NOT domain data we
own (the User service owns membership). They exist because most of the
events we must email about carry only an account_id — invoice.paid,
payment.failed, usage.threshold_reached, credits.low_balance,
post.published/failed have no email address in the payload — and there is
no service-auth token to call the User service with (the same KNOWN GAP the
Scheduler and Social services recorded). So we remember addresses from the
events that DO carry them: user.registered (user_id -> email),
member.joined (account_id + user_id + email + role), and account.created
(account_id -> owner_user_id, email resolved lazily through known_users so
event ordering between our queues never matters). Same precedent as the
Scheduler's content_refs read model.

One placeholder-era wrinkle (see the Auth service): access tokens currently
carry account_id == user_id, so account-scoped events from Billing/Usage/
Credits/Social arrive with the USER id in the account_id field. Recipient
resolution therefore falls back to known_users[account_id] when no
account_recipients row matches — which covers today's reality with no code
change needed when Auth starts issuing real account ids.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text, UniqueConstraint
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


class NotificationLog(Base):
    """One row per concluded send attempt (see module docstring)."""
    __tablename__ = "notification_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # The consumed domain event this email answers — the join key back to the
    # producer's world and to our processed_events dedup row.
    event_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    routing_key: Mapped[str] = mapped_column(String(100), nullable=False)
    # Template name (verification | password_reset | invite | ... ) — the
    # spec's "type" field.
    template: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    # Tenant scope for the read routes. Nullable: a malformed event may not
    # name an account at all.
    account_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # Nullable: status=failed with recipient NULL means we could not resolve
    # an address for the account (see models docstring on the read model).
    recipient: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    # resend | mailgun — set only when a provider accepted the message.
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # sent | failed — terminal only.
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class KnownUser(Base):
    """user_id -> email, learned from user.registered / member.joined."""
    __tablename__ = "known_users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)


class AccountRecipient(Base):
    """Who to email for account-scoped alerts. email may be NULL when the
    membership event carried only a user_id (account.created) — resolution
    joins through known_users at send time."""
    __tablename__ = "account_recipients"
    __table_args__ = (UniqueConstraint("account_id", "user_id", name="uq_account_recipient_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # owner | admin | member — owner is preferred for alerts.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)
