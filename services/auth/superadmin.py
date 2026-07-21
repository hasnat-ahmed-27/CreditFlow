"""
Platform SuperAdmin designation (spec §4 and §8 Service 13: "SuperAdmin role
— platform-level, not account-scoped — can view/search across all accounts").

DESIGN — an allow-list of emails in the environment, reconciled into
`users.is_superadmin`:

  SUPERADMIN_EMAILS=ops@creditflow.dev,founder@creditflow.dev

Why an env var and not an endpoint: a promote-to-superadmin endpoint is a
privilege-escalation surface that must itself be guarded by a superadmin —
which leaves the bootstrap problem (who makes the first one?) and one bug
away from a self-service platform takeover. Deployment configuration has no
such surface: the list is set by whoever can deploy, which is already the
highest privilege in the system. Nothing in the request path can write this
flag.

Why RECONCILE (grant AND revoke) rather than grant-only: the env var is the
single source of truth. Removing an address from the list must actually
remove the privilege on the next restart — a grant-only seed would leave a
former operator permanently super with no way to demote them short of hand-
editing Postgres. `sync()` therefore drives the flag in BOTH directions and
revokes the live sessions of anyone it demotes, so a stale token minted
seconds earlier stops working immediately rather than riding out its TTL.

Emails are matched case-insensitively and stored lower-cased, matching how
routes.py normalises every email it touches.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from creditflow_common import config

import store
from models import RefreshToken, User, utcnow

logger = logging.getLogger("auth.superadmin")

# The role claim a designated user's JWT carries. The Admin service already
# gates on exactly this string (services/admin/routes.py SUPERADMIN_ROLE).
SUPERADMIN_ROLE = "superadmin"


def designated_emails() -> set[str]:
    """Parse SUPERADMIN_EMAILS. Empty (the default) means the platform has no
    SuperAdmin — a deployment must opt in explicitly."""
    raw = config.env("SUPERADMIN_EMAILS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def is_designated(email: str) -> bool:
    """Used at signup so an operator whose address is already listed becomes
    super the moment they register, without waiting for a restart."""
    return email.lower() in designated_emails()


def role_for(user: User, account_role: str) -> str:
    """The role claim to mint. The platform role OUTRANKS the account role:
    a SuperAdmin is super in every account-scoped token they hold, which is
    what makes the Admin console's cross-account views reachable (§8/13).
    They still carry a real account_id, so account-scoped services keep
    working normally for them."""
    return SUPERADMIN_ROLE if user.is_superadmin else account_role


def sync(db: Session) -> dict[str, int]:
    """Reconcile users.is_superadmin against SUPERADMIN_EMAILS. Called on
    startup (main.py). Returns {"granted": n, "revoked": n}."""
    emails = designated_emails()
    granted = revoked = 0

    for user in db.scalars(select(User).where(User.email.in_(emails))).all() if emails else []:
        if not user.is_superadmin:
            user.is_superadmin = True
            granted += 1
            logger.info("granted superadmin to %s", user.email)

    stale = select(User).where(User.is_superadmin.is_(True))
    if emails:
        stale = stale.where(User.email.not_in(emails))
    for user in db.scalars(stale).all():
        user.is_superadmin = False
        revoked += 1
        # A demotion must bite NOW, not when the token expires: kill every
        # live session so the next request re-authenticates without the role.
        _revoke_sessions(db, user.id)
        logger.warning("revoked superadmin from %s (no longer in SUPERADMIN_EMAILS)", user.email)

    db.commit()
    if granted or revoked:
        logger.info("superadmin sync: %d granted, %d revoked", granted, revoked)
    return {"granted": granted, "revoked": revoked}


def _revoke_sessions(db: Session, user_id: str) -> None:
    """Same teardown routes.py does on password reset — refresh rows revoked
    and their paired access jtis dropped from Redis."""
    rows = db.scalars(
        select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
    ).all()
    for row in rows:
        row.revoked_at = utcnow()
        if row.access_jti:
            store.revoke_session(row.access_jti)
