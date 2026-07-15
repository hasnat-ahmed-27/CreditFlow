"""
Scheduler endpoints — the account's content calendar.

Authorization model (same as Content/AI/Usage/Credits): identity comes from
the RS256 access token, verified with the shared public key. Schedules are
scoped by the token's account_id claim — cross-account access answers 404,
never 403, so schedule ids don't leak existence.

Role model mirrors the Content service's status machine: putting content on
the calendar (or moving/cancelling it) is what ultimately publishes it, so
create/reschedule/cancel require a publish role (owner/admin) — the same gate
Content puts on its approved -> scheduled transition. Reading the calendar is
any member's business.

Content validation happens against the content_refs read model consumer.py
maintains from content.* events (no synchronous call into the Content
service): the content must exist, belong to the caller's account (404
otherwise, like everything tenant-scoped), and be in the `approved` or
`scheduled` Content-service status — drafts haven't passed the approval gate
and published posts already went out (409 either way).

KNOWN GAP (cross-service wiring): the Content service expects the Scheduler
to advance content approved -> scheduled via POST /content/{id}/status, but
that call needs a publish-role token and there is no service-auth story yet
(see the Auth placeholder note) — so the Content-side status is NOT advanced
here. The schedule rows in this service are the source of truth for the
calendar until that wiring lands.

Semantics worth naming: DELETE /schedules/{id} is a CANCEL, not a hard
delete — the row stays (status=cancelled) so the calendar keeps its history;
it returns the cancelled schedule, not 204. Only pending schedules can be
rescheduled or cancelled; `fired` and `cancelled` are terminal.

Times: stored and returned in UTC (ISO 8601 with offset); the spec has the
FRONTEND convert for calendar display, so responses carry the schedule's
IANA `timezone` alongside. GET /schedules/calendar is the spec's month/week
view: everything for the account with publish_at inside [start, end).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creditflow_common import jwt_utils

import database
import schemas
from models import ContentRef, ScheduledPost, as_utc, utcnow

router = APIRouter(tags=["scheduler"])

# Roles that may alter the calendar (spec roles: owner / admin / member).
PUBLISH_ROLES = ("owner", "admin")

# Content-service statuses that may be placed on the calendar.
SCHEDULABLE_CONTENT_STATUSES = ("approved", "scheduled")


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


def _owned(schedule_id: str, claims: dict, db: Session) -> ScheduledPost:
    row = db.get(ScheduledPost, schedule_id)
    if row is None or row.account_id != claims["account_id"]:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return row


def _require_publish_role(claims: dict) -> None:
    if claims.get("role") not in PUBLISH_ROLES:
        raise HTTPException(status_code=403, detail="Requires a role with publish permission")


def _to_utc(dt: datetime, tz_name: str) -> datetime:
    """Client datetimes: aware -> convert; naive -> interpret in tz_name."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(timezone.utc)


def _require_future(publish_at_utc: datetime) -> datetime:
    if publish_at_utc <= utcnow():
        raise HTTPException(status_code=422, detail="publish_at must be in the future")
    return publish_at_utc


def _schedulable_content(content_id: str, claims: dict, db: Session) -> ContentRef:
    ref = db.get(ContentRef, content_id)
    if ref is None or ref.account_id != claims["account_id"]:
        raise HTTPException(status_code=404, detail="Content not found")
    if ref.status not in SCHEDULABLE_CONTENT_STATUSES:
        raise HTTPException(status_code=409, detail={
            "message": f"Content in status {ref.status!r} cannot be scheduled",
            "content_status": ref.status,
            "schedulable": list(SCHEDULABLE_CONTENT_STATUSES),
        })
    return ref


def _item_dict(row: ScheduledPost) -> dict:
    return {
        "schedule_id": row.id,
        "account_id": row.account_id,
        "content_id": row.content_id,
        "created_by_user_id": row.created_by_user_id,
        "publish_at": as_utc(row.publish_at).isoformat(),
        "timezone": row.timezone,
        "recurrence": row.recurrence,
        "status": row.status,
        "fire_count": row.fire_count,
        "last_fired_at": as_utc(row.last_fired_at).isoformat() if row.last_fired_at else None,
        "created_at": as_utc(row.created_at).isoformat() if row.created_at else None,
        "updated_at": as_utc(row.updated_at).isoformat() if row.updated_at else None,
    }


# --------------------------------------------------------------------------
# Create / list / calendar / get
# --------------------------------------------------------------------------

@router.post("/schedules", status_code=201)
def create_schedule(
    body: schemas.ScheduleCreate,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    _require_publish_role(claims)
    _schedulable_content(body.content_id, claims, db)
    publish_at = _require_future(_to_utc(body.publish_at, body.timezone))

    row = ScheduledPost(
        account_id=claims["account_id"],
        content_id=body.content_id,
        created_by_user_id=claims.get("sub"),
        publish_at=publish_at,
        timezone=body.timezone,
        recurrence=body.recurrence,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _item_dict(row)


@router.get("/schedules")
def list_schedules(
    status: str | None = Query(default=None),
    content_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    if status is not None and status not in schemas.STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {schemas.STATUSES}")

    where = [ScheduledPost.account_id == claims["account_id"]]
    if status is not None:
        where.append(ScheduledPost.status == status)
    if content_id is not None:
        where.append(ScheduledPost.content_id == content_id)

    total = db.scalar(select(func.count()).select_from(ScheduledPost).where(*where))
    rows = db.scalars(
        select(ScheduledPost).where(*where)
        .order_by(ScheduledPost.publish_at, ScheduledPost.id)
        .limit(limit).offset(offset)
    ).all()
    return {"items": [_item_dict(r) for r in rows],
            "total": total, "limit": limit, "offset": offset}


# NOTE: declared before /schedules/{schedule_id} so "calendar" never matches
# as a schedule id.
@router.get("/schedules/calendar")
def calendar(
    start: datetime = Query(...),
    end: datetime = Query(...),
    status: str | None = Query(default=None),
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Spec's month/week view: every schedule whose (next or final) publish_at
    falls in [start, end). Naive bounds are taken as UTC."""
    start_utc = _to_utc(start, "UTC")
    end_utc = _to_utc(end, "UTC")
    if start_utc >= end_utc:
        raise HTTPException(status_code=422, detail="start must be before end")
    if status is not None and status not in schemas.STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {schemas.STATUSES}")

    where = [
        ScheduledPost.account_id == claims["account_id"],
        ScheduledPost.publish_at >= start_utc,
        ScheduledPost.publish_at < end_utc,
    ]
    if status is not None:
        where.append(ScheduledPost.status == status)

    rows = db.scalars(
        select(ScheduledPost).where(*where)
        .order_by(ScheduledPost.publish_at, ScheduledPost.id)
    ).all()
    return {"start": start_utc.isoformat(), "end": end_utc.isoformat(),
            "items": [_item_dict(r) for r in rows]}


@router.get("/schedules/{schedule_id}")
def get_schedule(
    schedule_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    return _item_dict(_owned(schedule_id, claims, db))


# --------------------------------------------------------------------------
# Reschedule / cancel
# --------------------------------------------------------------------------

@router.patch("/schedules/{schedule_id}")
def reschedule(
    schedule_id: str,
    body: schemas.ScheduleUpdate,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    row = _owned(schedule_id, claims, db)  # tenant 404 before the role 403
    _require_publish_role(claims)
    if row.status != "pending":
        raise HTTPException(status_code=409, detail={
            "message": f"Cannot reschedule a {row.status} schedule",
            "status": row.status,
        })

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update")

    if "timezone" in fields:
        row.timezone = fields["timezone"]
    if "recurrence" in fields:  # explicit null clears -> one-off
        row.recurrence = fields["recurrence"]
    if "publish_at" in fields:
        row.publish_at = _require_future(_to_utc(fields["publish_at"], row.timezone))

    db.commit()
    db.refresh(row)
    return _item_dict(row)


@router.delete("/schedules/{schedule_id}")
def cancel_schedule(
    schedule_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Soft cancel: the row stays for calendar history (see module docstring)."""
    row = _owned(schedule_id, claims, db)
    _require_publish_role(claims)
    if row.status != "pending":
        raise HTTPException(status_code=409, detail={
            "message": f"Cannot cancel a {row.status} schedule",
            "status": row.status,
        })
    row.status = "cancelled"
    db.commit()
    db.refresh(row)
    return _item_dict(row)
