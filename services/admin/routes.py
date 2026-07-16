"""
Admin/Ops REST surface — the spec's "expose the data this service surfaces
via REST for the frontend admin console": audit-log timeline, active-session
viewer + revocation, cross-account directory, per-account aggregate
overview, and the suspend/reactivate oversight switches.

Authorization model (the spec's two admin tiers, on top of the same
verify-only RS256 scheme every service uses — this service never mints):

  EVERY route requires an admin-tier role claim. A valid token whose role is
  not owner/admin/superadmin (i.e. `member`, or anything else) answers 403 —
  this service has no non-admin surface at all.

  - SuperAdmin (`role == "superadmin"`, platform-level, not account-scoped)
    can view/search across ALL accounts, and is the only tier that may
    suspend/reactivate or see the platform-wide user directory and stats.
  - TenantAdmin (`role == owner|admin` — the account-scoped manager roles the
    rest of the platform already enforces) is restricted to the token's own
    account_id: list routes are force-scoped, an EXPLICIT foreign account_id
    filter answers 403, and foreign resource paths answer 404 (ids don't
    leak existence — same convention as every other service).

Suspension publishes no events (spec: "Publishes: none") — it flips the
directory row's status and immediately revokes the target's live sessions in
Redis, which is the enforcement switch the whole platform already honors.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from creditflow_common import jwt_utils

import clients
import database
import sessions
from models import AccountDirectory, AuditLog, UserDirectory, as_utc, utcnow

logger = logging.getLogger("admin.routes")

router = APIRouter(prefix="/admin", tags=["admin"])

SUPERADMIN_ROLE = "superadmin"
TENANT_ADMIN_ROLES = ("owner", "admin")
ADMIN_ROLES = TENANT_ADMIN_ROLES + (SUPERADMIN_ROLE,)

STATUSES = ("active", "suspended")


class SuspendRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------
# Auth dependencies
# --------------------------------------------------------------------------

def bearer_token(authorization: str = Header(default="")) -> str:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.split(" ", 1)[1]


def current_claims(token: str = Depends(bearer_token)) -> dict:
    """Verify the Bearer access token's RS256 signature + expiry."""
    try:
        claims = jwt_utils.verify_token(token)
    except jwt_utils.TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if claims.get("type") != "access":
        raise HTTPException(status_code=401, detail="Not an access token")
    return claims


def admin_claims(claims: dict = Depends(current_claims)) -> dict:
    """The gate on EVERY route: a valid token without an admin-tier role is 403."""
    if claims.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Requires an admin role")
    return claims


def superadmin_claims(claims: dict = Depends(admin_claims)) -> dict:
    if claims.get("role") != SUPERADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Requires the superadmin role")
    return claims


def _is_super(claims: dict) -> bool:
    return claims.get("role") == SUPERADMIN_ROLE


def _scope_filter(claims: dict, account_id: str | None) -> str | None:
    """Resolve the account filter for a LIST route: superadmin may filter on
    any account (or none); a tenant admin is force-scoped to their own, and
    explicitly asking for a foreign one is refused outright."""
    if _is_super(claims):
        return account_id
    if account_id is not None and account_id != claims["account_id"]:
        raise HTTPException(status_code=403, detail="Restricted to your own account")
    return claims["account_id"]


def _require_own_account(claims: dict, account_id: str) -> None:
    """Resource-path scoping: a tenant admin addressing a foreign account
    gets 404, never 403 — ids don't leak existence."""
    if not _is_super(claims) and account_id != claims["account_id"]:
        raise HTTPException(status_code=404, detail="Account not found")


def _parse_when(value: str | None, param: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{param} must be an ISO-8601 datetime")
    # Naive input is taken as UTC — everything this service stores is UTC.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Audit log (read-only — the consumer is the only writer)
# --------------------------------------------------------------------------

def _audit_dict(row: AuditLog) -> dict:
    return {
        "audit_id": row.id,
        "event_id": row.event_id,
        "exchange": row.exchange,
        "routing_key": row.routing_key,
        "account_id": row.account_id,
        "actor_user_id": row.actor_user_id,
        "payload": json.loads(row.payload),
        "created_at": as_utc(row.created_at).isoformat() if row.created_at else None,
    }


@router.get("/audit-log")
def list_audit_log(
    account_id: str | None = Query(default=None),
    actor_user_id: str | None = Query(default=None),
    routing_key: str | None = Query(default=None, max_length=100),
    exchange: str | None = Query(default=None, max_length=50),
    since: str | None = Query(default=None, description="ISO-8601; naive = UTC"),
    until: str | None = Query(default=None, description="ISO-8601; naive = UTC"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: dict = Depends(admin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Searchable per-account timeline: who did what, when."""
    conditions = []
    scoped_account = _scope_filter(claims, account_id)
    if scoped_account is not None:
        conditions.append(AuditLog.account_id == scoped_account)
    if actor_user_id is not None:
        conditions.append(AuditLog.actor_user_id == actor_user_id)
    if routing_key is not None:
        conditions.append(AuditLog.routing_key == routing_key)
    if exchange is not None:
        conditions.append(AuditLog.exchange == exchange)
    if (since_dt := _parse_when(since, "since")) is not None:
        conditions.append(AuditLog.created_at >= since_dt)
    if (until_dt := _parse_when(until, "until")) is not None:
        conditions.append(AuditLog.created_at <= until_dt)

    total = db.scalar(select(func.count()).select_from(AuditLog).where(*conditions))
    rows = db.scalars(
        select(AuditLog).where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id)
        .limit(limit).offset(offset)
    ).all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_audit_dict(r) for r in rows]}


@router.get("/audit-log/{audit_id}")
def get_audit_entry(
    audit_id: str,
    claims: dict = Depends(admin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    row = db.get(AuditLog, audit_id)
    if row is None or (not _is_super(claims) and row.account_id != claims["account_id"]):
        raise HTTPException(status_code=404, detail="Audit entry not found")
    return _audit_dict(row)


# --------------------------------------------------------------------------
# Active sessions (Redis, live — see sessions.py)
# --------------------------------------------------------------------------

@router.get("/sessions")
def list_active_sessions(
    account_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    claims: dict = Depends(admin_claims),
) -> dict:
    """Live jti sessions, straight from Redis (never a stale copy)."""
    items = sessions.list_sessions(account_id=_scope_filter(claims, account_id),
                                   user_id=user_id)
    return {"total": len(items), "items": items}


@router.delete("/sessions/{jti}")
def revoke_active_session(
    jti: str,
    claims: dict = Depends(admin_claims),
) -> dict:
    """Force logout: delete the jti from Redis — the token is invalid at the
    Gateway from this moment, before its natural expiry."""
    session = sessions.get_session(jti)
    if session is None or (not _is_super(claims)
                           and session["account_id"] != claims["account_id"]):
        raise HTTPException(status_code=404, detail="Session not found")
    sessions.revoke_session(jti)
    logger.info("session %s revoked by %s", jti, claims["sub"])
    return {"jti": jti, "revoked": True}


# --------------------------------------------------------------------------
# Account directory + aggregate overview
# --------------------------------------------------------------------------

def _account_dict(row: AccountDirectory) -> dict:
    return {
        "account_id": row.account_id,
        "name": row.name,
        "type": row.type,
        "plan_tier": row.plan_tier,
        "owner_user_id": row.owner_user_id,
        "status": row.status,
        "suspended_at": as_utc(row.suspended_at).isoformat() if row.suspended_at else None,
        "suspended_by": row.suspended_by,
        "suspend_reason": row.suspend_reason,
        "first_seen_at": as_utc(row.created_at).isoformat() if row.created_at else None,
    }


@router.get("/accounts")
def list_accounts(
    q: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: dict = Depends(admin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Cross-account directory (SuperAdmin); a tenant admin sees only their
    own account's row."""
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {STATUSES}")
    conditions = []
    if not _is_super(claims):
        conditions.append(AccountDirectory.account_id == claims["account_id"])
    if status is not None:
        conditions.append(AccountDirectory.status == status)
    if q:
        conditions.append(or_(AccountDirectory.account_id == q,
                              AccountDirectory.name.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(AccountDirectory).where(*conditions))
    rows = db.scalars(
        select(AccountDirectory).where(*conditions)
        .order_by(AccountDirectory.created_at.desc(), AccountDirectory.account_id)
        .limit(limit).offset(offset)
    ).all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_account_dict(r) for r in rows]}


def _get_account_or_404(db: Session, claims: dict, account_id: str) -> AccountDirectory:
    _require_own_account(claims, account_id)
    row = db.get(AccountDirectory, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return row


@router.get("/accounts/{account_id}")
def get_account(
    account_id: str,
    claims: dict = Depends(admin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    return _account_dict(_get_account_or_404(db, claims, account_id))


@router.get("/accounts/{account_id}/overview")
def account_overview(
    account_id: str,
    claims: dict = Depends(admin_claims),
    token: str = Depends(bearer_token),
    db: Session = Depends(database.get_db),
) -> dict:
    """Aggregate per-account view (spec): plan tier, credit balance, usage
    this period, active members — pulled live from the User/Credits/Usage
    services with the caller's own bearer (read-only; see clients.py for the
    service-auth KNOWN GAP). Each section degrades independently: a
    downstream failure lands the service's name in `degraded` instead of
    failing the whole view."""
    _require_own_account(claims, account_id)
    directory = db.get(AccountDirectory, account_id)

    overview: dict = {
        "account_id": account_id,
        "directory": _account_dict(directory) if directory else None,
        "active_sessions": len(sessions.list_sessions(account_id=account_id)),
        "profile": None, "members": None, "credits": None, "usage": None,
        "degraded": [],
    }
    calls = (
        ("profile", "user", lambda: clients.get_account(account_id, token)),
        ("members", "user", lambda: clients.get_members(account_id, token)),
        ("credits", "credits", lambda: clients.get_credit_balance(token)),
        ("usage", "usage", lambda: clients.get_usage_summary(token)),
    )
    for section, service, call in calls:
        try:
            overview[section] = call()
        except clients.ClientError as exc:
            logger.warning("overview %s: %s section degraded: %s", account_id, section, exc)
            if service not in overview["degraded"]:
                overview["degraded"].append(service)
    return overview


@router.post("/accounts/{account_id}/suspend")
def suspend_account(
    account_id: str,
    body: SuspendRequest | None = None,
    claims: dict = Depends(superadmin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Suspend a tenant: flip the directory row and force-logout every live
    session on the account. Idempotent — suspending twice stays suspended."""
    row = _get_account_or_404(db, claims, account_id)
    row.status = "suspended"
    row.suspended_at = utcnow()
    row.suspended_by = claims["sub"]
    row.suspend_reason = body.reason if body else None
    db.commit()
    revoked = sessions.revoke_matching(account_id=account_id)
    logger.info("account %s suspended by %s (%d sessions revoked)",
                account_id, claims["sub"], revoked)
    return {"account_id": account_id, "status": "suspended", "sessions_revoked": revoked}


@router.post("/accounts/{account_id}/reactivate")
def reactivate_account(
    account_id: str,
    claims: dict = Depends(superadmin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    row = _get_account_or_404(db, claims, account_id)
    row.status = "active"
    row.suspended_at = None
    row.suspended_by = None
    row.suspend_reason = None
    db.commit()
    logger.info("account %s reactivated by %s", account_id, claims["sub"])
    return {"account_id": account_id, "status": "active"}


# --------------------------------------------------------------------------
# Platform user directory (SuperAdmin only — cross-account by nature)
# --------------------------------------------------------------------------

def _user_dict(row: UserDirectory) -> dict:
    return {
        "user_id": row.user_id,
        "email": row.email,
        "status": row.status,
        "suspended_at": as_utc(row.suspended_at).isoformat() if row.suspended_at else None,
        "suspended_by": row.suspended_by,
        "suspend_reason": row.suspend_reason,
        "first_seen_at": as_utc(row.created_at).isoformat() if row.created_at else None,
    }


@router.get("/users")
def list_users(
    q: str | None = Query(default=None, max_length=320),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: dict = Depends(superadmin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {STATUSES}")
    conditions = []
    if status is not None:
        conditions.append(UserDirectory.status == status)
    if q:
        conditions.append(or_(UserDirectory.user_id == q,
                              UserDirectory.email.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(UserDirectory).where(*conditions))
    rows = db.scalars(
        select(UserDirectory).where(*conditions)
        .order_by(UserDirectory.created_at.desc(), UserDirectory.user_id)
        .limit(limit).offset(offset)
    ).all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_user_dict(r) for r in rows]}


def _get_user_or_404(db: Session, user_id: str) -> UserDirectory:
    row = db.get(UserDirectory, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return row


@router.get("/users/{user_id}")
def get_user(
    user_id: str,
    claims: dict = Depends(superadmin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    return _user_dict(_get_user_or_404(db, user_id))


@router.post("/users/{user_id}/suspend")
def suspend_user(
    user_id: str,
    body: SuspendRequest | None = None,
    claims: dict = Depends(superadmin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Suspend a user platform-wide: flip the directory row and force-logout
    every live session the user holds (across all their accounts)."""
    row = _get_user_or_404(db, user_id)
    row.status = "suspended"
    row.suspended_at = utcnow()
    row.suspended_by = claims["sub"]
    row.suspend_reason = body.reason if body else None
    db.commit()
    revoked = sessions.revoke_matching(user_id=user_id)
    logger.info("user %s suspended by %s (%d sessions revoked)",
                user_id, claims["sub"], revoked)
    return {"user_id": user_id, "status": "suspended", "sessions_revoked": revoked}


@router.post("/users/{user_id}/reactivate")
def reactivate_user(
    user_id: str,
    claims: dict = Depends(superadmin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    row = _get_user_or_404(db, user_id)
    row.status = "active"
    row.suspended_at = None
    row.suspended_by = None
    row.suspend_reason = None
    db.commit()
    logger.info("user %s reactivated by %s", user_id, claims["sub"])
    return {"user_id": user_id, "status": "active"}


# --------------------------------------------------------------------------
# Platform stats (SuperAdmin only — "inspect system state")
# --------------------------------------------------------------------------

@router.get("/stats")
def platform_stats(
    claims: dict = Depends(superadmin_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    def _count(model, *conditions) -> int:
        return db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0

    return {
        "accounts": {
            "total": _count(AccountDirectory),
            "suspended": _count(AccountDirectory, AccountDirectory.status == "suspended"),
        },
        "users": {
            "total": _count(UserDirectory),
            "suspended": _count(UserDirectory, UserDirectory.status == "suspended"),
        },
        "audit_events": _count(AuditLog),
        "active_sessions": len(sessions.list_sessions()),
    }
