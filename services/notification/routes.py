"""
Notification read endpoints — the spec's audit surface over notification_log
("mostly a consumer, minimal REST surface").

Authorization model (same as Social/Scheduler/Content/AI/Usage/Credits):
identity comes from the RS256 access token, verified with the shared public
key — this service only ever VERIFIES, it never mints. Everything is scoped
by the token's account_id claim — cross-account access answers 404, never
403, so ids don't leak existence. Any member may read the account's
notification history (it is their account's mail, not a mutation).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creditflow_common import jwt_utils

import database
from models import NotificationLog, as_utc

logger = logging.getLogger("notification.routes")

router = APIRouter(tags=["notification"])

STATUSES = ("sent", "failed")


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


def _log_dict(row: NotificationLog) -> dict:
    return {
        "notification_id": row.id,
        "event_id": row.event_id,
        "routing_key": row.routing_key,
        "template": row.template,
        "account_id": row.account_id,
        "recipient": row.recipient,
        "subject": row.subject,
        "provider": row.provider,
        "provider_message_id": row.provider_message_id,
        "status": row.status,
        "error": row.error,
        "created_at": as_utc(row.created_at).isoformat() if row.created_at else None,
    }


@router.get("/notifications")
def list_notifications(
    status: str | None = Query(default=None),
    template: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {STATUSES}")
    conditions = [NotificationLog.account_id == claims["account_id"]]
    if status is not None:
        conditions.append(NotificationLog.status == status)
    if template is not None:
        conditions.append(NotificationLog.template == template)
    total = db.scalar(select(func.count()).select_from(NotificationLog).where(*conditions))
    rows = db.scalars(
        select(NotificationLog).where(*conditions)
        .order_by(NotificationLog.created_at.desc(), NotificationLog.id)
        .limit(limit).offset(offset)
    ).all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_log_dict(r) for r in rows]}


@router.get("/notifications/{notification_id}")
def get_notification(
    notification_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    row = db.get(NotificationLog, notification_id)
    if row is None or row.account_id != claims["account_id"]:
        raise HTTPException(status_code=404, detail="Notification not found")
    return _log_dict(row)
