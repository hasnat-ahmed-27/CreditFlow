"""
Individual-account provisioning — the ONE implementation of spec §8 Service 3's
"create an Account automatically on signup (type: individual)".

Two callers reach it, deliberately:
  - consumer.py, on `user.registered` from Auth (the event-driven path);
  - internal.py, on Auth's synchronous POST /internal/accounts/individual at
    signup (the path that makes the account exist BEFORE the first login, so
    login can mint a real account-scoped JWT rather than guessing).

Both paths run the same user_id business-key guard, so whichever arrives
first wins and the second is a no-op — the same "processed_events plus a
business key" belt-and-braces the credits/usage consumers use. Callers own
the transaction and the post-commit publish; this module only stages rows.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Account, AccountMember

logger = logging.getLogger("user.provisioning")

# The role the signup user holds in their own individual account (spec §6:
# "an Account of type 'individual' with exactly one member (Owner)").
INDIVIDUAL_OWNER_ROLE = "owner"


def find_individual_account(db: Session, user_id: str) -> Account | None:
    """The individual account this user owns, if it exists — the business key
    that makes provisioning idempotent across BOTH callers."""
    return db.scalar(
        select(Account)
        .join(AccountMember, AccountMember.account_id == Account.id)
        .where(
            Account.type == "individual",
            AccountMember.user_id == user_id,
            AccountMember.role == INDIVIDUAL_OWNER_ROLE,
        )
    )


def ensure_individual_account(db: Session, user_id: str, email: str = "") -> tuple[Account, bool]:
    """Return (account, created). Stages the rows in the caller's session —
    the caller commits and, if created is True, publishes account.created."""
    existing = find_individual_account(db, user_id)
    if existing is not None:
        logger.info("user %s already has individual account %s — skipping", user_id, existing.id)
        return existing, False

    account = Account(
        type="individual",
        name=email or user_id,
        plan_tier="free",
        created_by_user_id=user_id,
    )
    db.add(account)
    db.flush()  # assigns account.id for the membership row
    db.add(AccountMember(account_id=account.id, user_id=user_id, role=INDIVIDUAL_OWNER_ROLE))
    logger.info("created individual account %s for user %s", account.id, user_id)
    return account, True


def account_created_event(account: Account, user_id: str) -> tuple[str, dict]:
    """The account.created payload both callers publish after committing."""
    return ("account.created", {
        "account_id": account.id,
        "type": account.type,
        "name": account.name,
        "plan_tier": account.plan_tier,
        "owner_user_id": user_id,
    })
