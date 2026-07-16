"""
Recipients read model — learns "who to email for account X" from the events
that carry addresses, so account-scoped alerts (billing/usage/credits/social)
can be delivered even though their payloads name only an account_id. See
models.py for why this exists (no service-auth token to ask the User service)
and for the placeholder-JWT fallback documented on resolve_account_email.

apply_event() is called by the consumer BEFORE rendering, inside the same
transaction as the processed_events row — so the mapping and the dedup
record land (or roll back) together.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AccountRecipient, KnownUser

logger = logging.getLogger("notification.recipients")


def apply_event(db: Session, routing_key: str, data: dict) -> None:
    """Upsert read-model rows for membership events; a no-op for the rest."""
    if routing_key == "user.registered":
        _upsert_user(db, data.get("user_id"), data.get("email"))
    elif routing_key == "member.joined":
        _upsert_user(db, data.get("user_id"), data.get("email"))
        _upsert_recipient(db, data.get("account_id"), data.get("user_id"),
                          email=data.get("email"), role=data.get("role") or "member")
    elif routing_key == "account.created":
        # Carries owner_user_id but no email — stored with email=NULL and
        # resolved lazily through known_users, so it does not matter whether
        # this or the matching user.registered is consumed first.
        _upsert_recipient(db, data.get("account_id"), data.get("owner_user_id"),
                          email=None, role="owner")


def _upsert_user(db: Session, user_id: str | None, email: str | None) -> None:
    if not user_id or not email:
        return
    row = db.get(KnownUser, user_id)
    if row is None:
        db.add(KnownUser(user_id=user_id, email=email))
    else:
        row.email = email


def _upsert_recipient(db: Session, account_id: str | None, user_id: str | None,
                      email: str | None, role: str) -> None:
    if not account_id or not user_id:
        return
    row = db.scalar(select(AccountRecipient).where(
        AccountRecipient.account_id == account_id,
        AccountRecipient.user_id == user_id,
    ))
    if row is None:
        db.add(AccountRecipient(account_id=account_id, user_id=user_id,
                                email=email, role=role))
    else:
        if email:
            row.email = email
        row.role = role


def resolve_account_email(db: Session, account_id: str | None) -> str | None:
    """The address account-scoped alerts go to: the account's owner (earliest
    row wins on a tie), any other member as a fallback — joining through
    known_users when the recipient row has no email of its own. If no
    recipient row matches at all, fall back to known_users[account_id]
    directly: Auth's placeholder tokens put the USER id in account_id, so
    events scoped by those tokens resolve through the user mapping."""
    if not account_id:
        return None
    rows = db.scalars(
        select(AccountRecipient)
        .where(AccountRecipient.account_id == account_id)
        .order_by(AccountRecipient.created_at, AccountRecipient.id)
    ).all()
    ordered = [r for r in rows if r.role == "owner"] + [r for r in rows if r.role != "owner"]
    for row in ordered:
        if row.email:
            return row.email
        if row.user_id:
            user = db.get(KnownUser, row.user_id)
            if user is not None and user.email:
                return user.email
    user = db.get(KnownUser, account_id)  # placeholder account_id == user_id
    if user is not None and user.email:
        return user.email
    return None
