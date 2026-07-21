"""
Internal service-to-service API — the seam the Auth service calls to learn
what belongs in a JWT (spec §6: "JWT must carry user_id, account_id, role
and jti"), which this service alone owns.

WHY SYNCHRONOUS, not an event-fed read model in Auth:
  account_members is the source of truth for roles, and it is THIS service's
  table (spec §8 Service 3 — "Database Ownership: accounts, account_members,
  invites"). A read model in Auth would duplicate that ownership and, worse,
  be eventually consistent at the exact instant it matters most: the moment a
  token is minted. A member demoted a second ago must not be handed an
  `admin` token because the projection hasn't caught up. So Auth asks us,
  the same way the AI service asks Usage for a quota verdict before spending
  money (services/ai/usage_client.py) — the repo's existing pattern for
  "decisions that must be current".
  The event path stays too: consumer.py still provisions individual accounts
  from `user.registered`, so a signup that happened while this service was
  down is healed asynchronously. Both paths share provisioning.py and are
  idempotent, so whichever wins, the result is identical.

WHY UNAUTHENTICATED:
  Auth calls POST /accounts/individual DURING signup — before any token for
  that user exists, so there is nothing to present. These routes live under
  the `/internal` prefix, which is deliberately ABSENT from the Gateway's
  ROUTE_TABLE (services/gateway/proxy.py): the only way to reach them is
  from inside the compose network. KNOWN GAP, shared with admin/clients.py
  and social: the platform has no service-auth token yet. When one lands,
  require it here — no route shapes change.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import database
import events
import provisioning
import schemas
from models import Account
from routes import get_membership

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/accounts/individual", status_code=201)
def ensure_individual_account(
    body: schemas.EnsureIndividualAccountRequest,
    db: Session = Depends(database.get_db),
) -> dict:
    """Idempotent: create the signup's individual account (with the user as
    its Owner) if it doesn't exist, and either way answer with the account_id
    and role Auth must put in the token. Called at signup AND at login, so a
    user whose `user.registered` event was lost still gets a real
    account-scoped JWT instead of a placeholder."""
    account, created = provisioning.ensure_individual_account(db, body.user_id, body.email or "")
    db.commit()
    if created:
        # Publish only after the commit — never announce a rolled-back write.
        events.publish(*provisioning.account_created_event(account, body.user_id))
    return {
        "user_id": body.user_id,
        "account_id": account.id,
        "role": provisioning.INDIVIDUAL_OWNER_ROLE,
        "type": account.type,
        "name": account.name,
        "plan_tier": account.plan_tier,
        "created": created,
    }


@router.get("/users/{user_id}/accounts/{account_id}")
def membership(
    user_id: str,
    account_id: str,
    db: Session = Depends(database.get_db),
) -> dict:
    """The authoritative answer to "may this user hold a token scoped to this
    account, and as what?". Auth calls it for the account-switch endpoint
    (reject non-members) and on refresh (re-resolve the role so a demotion or
    removal takes effect on the next rotation instead of riding out the
    refresh token's lifetime). 404 means "not a member" — a caller cannot
    distinguish that from "no such account", so ids leak nothing."""
    member = get_membership(db, account_id, user_id)
    account = db.get(Account, account_id) if member is not None else None
    if member is None or account is None:
        raise HTTPException(status_code=404, detail="Not a member of this account")
    return {
        "user_id": user_id,
        "account_id": account.id,
        "role": member.role,
        "type": account.type,
        "name": account.name,
        "plan_tier": account.plan_tier,
    }
