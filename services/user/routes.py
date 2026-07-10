"""
User/Tenant endpoints.

Authorization model:
  - Identity comes from the RS256 access token, verified with the shared
    public key via creditflow_common.jwt_utils (Auth alone holds the private
    key). Revocation (jti-in-Redis) is enforced at the Gateway/Auth layer,
    not re-checked here.
  - The caller's ROLE for a given account is resolved from account_members,
    not trusted from the token: the role claim is a snapshot from login and
    goes stale the moment someone is demoted or removed. This table is the
    source of truth the JWT claim is minted FROM.

Role hierarchy (owner > admin > member):
  - owner/admin can invite, change roles, and remove members;
  - admin cannot touch the owner or a fellow admin — only the owner manages
    admins;
  - nobody can grant or hold a second "owner" role: ownership is fixed at
    account creation (transfer would be its own deliberate flow).
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creditflow_common import config, jwt_utils

import database
import events
import schemas
import security
from models import Account, AccountMember, Invite, as_aware, utcnow

router = APIRouter(tags=["user"])

INVITE_TTL_SECONDS = int(config.env("USER_INVITE_TTL_SECONDS", str(7 * 24 * 3600)))
# Dev convenience: echo the raw invite token in the response so the flow can be
# tested with curl before the Notification service exists. NEVER enable in prod.
EXPOSE_DEV_TOKENS = config.env("USER_EXPOSE_DEV_TOKENS", "0") == "1"


def current_claims(authorization: str = Header(default="")) -> dict:
    """Verify the Bearer access token's RS256 signature + expiry."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        claims = jwt_utils.verify_token(token)
    except jwt_utils.TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if claims.get("type") != "access":
        raise HTTPException(status_code=401, detail="Not an access token")
    return claims


def get_membership(db: Session, account_id: str, user_id: str) -> AccountMember | None:
    return db.scalar(select(AccountMember).where(
        AccountMember.account_id == account_id, AccountMember.user_id == user_id
    ))


def require_member(db: Session, account_id: str, claims: dict) -> AccountMember:
    """404 for unknown accounts, 403 for non-members (don't leak existence
    beyond what a member could learn anyway)."""
    if db.get(Account, account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found")
    member = get_membership(db, account_id, claims["sub"])
    if member is None:
        raise HTTPException(status_code=403, detail="Not a member of this account")
    return member


def require_manager(db: Session, account_id: str, claims: dict) -> AccountMember:
    member = require_member(db, account_id, claims)
    if member.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Requires owner or admin role")
    return member


def _seat_count(db: Session, account_id: str) -> int:
    return db.scalar(
        select(func.count()).select_from(AccountMember).where(AccountMember.account_id == account_id)
    )


def _account_profile(db: Session, account: Account) -> dict:
    return {
        "account_id": account.id,
        "type": account.type,
        "name": account.name,
        "plan_tier": account.plan_tier,
        "seat_count": _seat_count(db, account.id),
        "created_at": account.created_at.isoformat(),
    }


# --------------------------------------------------------------------------
# Accounts: create team, profile, update
# --------------------------------------------------------------------------

@router.post("/accounts", status_code=201)
def create_team(
    body: schemas.CreateTeamRequest,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Explicit create-team flow — the caller becomes the team's owner.
    (Individual accounts are never created here; they come from the
    user.registered consumer.)"""
    account = Account(type="team", name=body.name, plan_tier="free", created_by_user_id=claims["sub"])
    db.add(account)
    db.flush()
    db.add(AccountMember(account_id=account.id, user_id=claims["sub"], role="owner"))
    db.commit()

    events.publish("account.created", {
        "account_id": account.id,
        "type": "team",
        "name": account.name,
        "plan_tier": account.plan_tier,
        "owner_user_id": claims["sub"],
    })
    return _account_profile(db, account)


@router.get("/accounts/{account_id}")
def account_profile(
    account_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Dashboard-header data: name, plan tier, seat count. Members only."""
    require_member(db, account_id, claims)
    return _account_profile(db, db.get(Account, account_id))


@router.patch("/accounts/{account_id}")
def update_account(
    account_id: str,
    body: schemas.UpdateAccountRequest,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Rename the account (owner/admin). plan_tier is deliberately NOT
    editable here — it changes via Billing's invoice.paid event."""
    require_manager(db, account_id, claims)
    account = db.get(Account, account_id)
    account.name = body.name
    db.commit()
    events.publish("account.updated", {
        "account_id": account.id,
        "change": "profile",
        "name": account.name,
        "updated_by_user_id": claims["sub"],
    })
    return _account_profile(db, account)


# --------------------------------------------------------------------------
# Team invites
# --------------------------------------------------------------------------

@router.post("/accounts/{account_id}/invites", status_code=201)
def create_invite(
    account_id: str,
    body: schemas.InviteRequest,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Owner/admin invites an email address into the team. We store only the
    token's hash; the raw token rides the invite.created event so the
    Notification service can email the link."""
    require_manager(db, account_id, claims)
    account = db.get(Account, account_id)
    if account.type != "team":
        # Spec §6: an individual account has exactly one member — inviting
        # into one would silently turn it into a team.
        raise HTTPException(status_code=400, detail="Only team accounts can invite members")

    email = body.email.lower()
    pending = db.scalar(select(Invite).where(
        Invite.account_id == account_id, Invite.email == email, Invite.status == "pending"
    ))
    if pending is not None and as_aware(pending.expires_at) > utcnow():
        raise HTTPException(status_code=409, detail="A pending invite for this email already exists")

    token = security.new_invite_token()
    invite = Invite(
        account_id=account_id,
        email=email,
        role=body.role,
        token_hash=security.hash_token(token),
        invited_by_user_id=claims["sub"],
        expires_at=utcnow() + timedelta(seconds=INVITE_TTL_SECONDS),
    )
    db.add(invite)
    db.commit()

    events.publish("invite.created", {
        "invite_id": invite.id,
        "account_id": account_id,
        "account_name": account.name,
        "email": email,
        "role": invite.role,
        "invite_token": token,
        "expires_at": invite.expires_at.isoformat(),
        "invited_by_user_id": claims["sub"],
    })

    resp = {"invite_id": invite.id, "email": email, "role": invite.role,
            "expires_at": invite.expires_at.isoformat(), "message": "Invite email queued"}
    if EXPOSE_DEV_TOKENS:
        resp["dev_invite_token"] = token
    return resp


@router.post("/invites/accept")
def accept_invite(
    body: schemas.AcceptInviteRequest,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """The invitee (logged in, so we know their user_id) redeems the emailed
    token: membership row is created and member.joined is published. The token
    is the capability — possession of the emailed link is what authorizes
    joining, matching how every invite email works."""
    invite = db.scalar(select(Invite).where(Invite.token_hash == security.hash_token(body.token)))
    if invite is None or invite.status != "pending" or as_aware(invite.expires_at) < utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired invite token")

    user_id = claims["sub"]
    if get_membership(db, invite.account_id, user_id) is not None:
        raise HTTPException(status_code=409, detail="Already a member of this account")

    invite.status = "accepted"  # single-use
    invite.accepted_by_user_id = user_id
    invite.accepted_at = utcnow()
    db.add(AccountMember(account_id=invite.account_id, user_id=user_id, role=invite.role))
    db.commit()

    account = db.get(Account, invite.account_id)
    events.publish("member.joined", {
        "account_id": invite.account_id,
        "account_name": account.name,
        "user_id": user_id,
        "email": invite.email,
        "role": invite.role,
    })
    return {"account_id": invite.account_id, "account_name": account.name, "role": invite.role,
            "message": "Invite accepted"}


# --------------------------------------------------------------------------
# Account switcher + Auth integration
# --------------------------------------------------------------------------

@router.get("/users/me/accounts")
def my_accounts(claims: dict = Depends(current_claims), db: Session = Depends(database.get_db)) -> dict:
    """Account switcher: every account the caller belongs to, with their role
    in each. The frontend picks one and asks Auth for a token scoped to it."""
    rows = db.execute(
        select(Account, AccountMember.role)
        .join(AccountMember, AccountMember.account_id == Account.id)
        .where(AccountMember.user_id == claims["sub"])
        .order_by(Account.created_at)
    ).all()
    return {
        "user_id": claims["sub"],
        "accounts": [
            {
                "account_id": account.id,
                "type": account.type,
                "name": account.name,
                "plan_tier": account.plan_tier,
                "role": role,
            }
            for account, role in rows
        ],
    }


@router.get("/users/{user_id}/default-account")
def default_account(user_id: str, db: Session = Depends(database.get_db)) -> dict:
    """Internal endpoint for the Auth service: the user's individual account
    (auto-created on signup) + role, so login can mint a real account-scoped
    JWT instead of its current account_id=user_id placeholder. Unauthenticated
    because Auth calls it BEFORE any token exists — the Gateway must not route
    external traffic here."""
    row = db.execute(
        select(Account, AccountMember.role)
        .join(AccountMember, AccountMember.account_id == Account.id)
        .where(
            AccountMember.user_id == user_id,
            Account.type == "individual",
            AccountMember.role == "owner",
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No individual account for this user")
    account, role = row
    return {"user_id": user_id, "account_id": account.id, "role": role}

