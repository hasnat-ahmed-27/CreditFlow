"""
Content endpoints — versioned CRUD + the server-enforced status machine.

Authorization model (same as AI/Usage/Credits): identity comes from the RS256
access token, verified with the shared public key. Content is scoped by the
token's account_id claim — spec §6: content is account-level data, editable
by any member; cross-account access answers 404, never 403, so content ids
don't leak existence.

Publish permission (spec: "publishable only by roles with publish
permission"): every STATUS TRANSITION — including approval, which is the gate
that makes content schedulable — requires the owner or admin role. Deleting
anything past draft is likewise gated (a member must not be able to yank an
approved/scheduled/published post out from under the pipeline). Members
create, edit, and delete drafts freely.

Lifecycle (TRANSITIONS): draft -> approved -> scheduled -> published, with
approved -> draft (revoke approval for rework), approved -> published
(immediate publish without scheduling), and scheduled -> approved (schedule
cancelled). published is terminal and the record becomes immutable — history
of what actually went out to LinkedIn must stay intact. `scheduled` and
`published` will normally be set by the Scheduler / Social Publishing
services calling this API; the machine doesn't care who the caller is, only
that the move is legal and the role may make it.

Event ordering (same rule as the other services): commit FIRST, then publish
— never announce a write that didn't land.
"""
from __future__ import annotations

import mimetypes
import os

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creditflow_common import jwt_utils

import database
import events
import schemas
import storage
from models import ContentItem, ContentVersion

router = APIRouter(tags=["content"])

# Roles with publish permission (spec roles: owner / admin / member).
PUBLISH_ROLES = ("owner", "admin")

# state -> the states it may legally move to (see module docstring).
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"approved"},
    "approved": {"draft", "scheduled", "published"},
    "scheduled": {"approved", "published"},
    "published": set(),
}

# Routing key per transition target; everything not listed is a plain update.
TRANSITION_EVENTS = {"approved": "content.approved", "published": "content.published"}

ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


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


def _owned(content_id: str, claims: dict, db: Session) -> ContentItem:
    item = db.get(ContentItem, content_id)
    if item is None or item.account_id != claims["account_id"]:
        raise HTTPException(status_code=404, detail="Content not found")
    return item


def _require_publish_role(claims: dict) -> None:
    if claims.get("role") not in PUBLISH_ROLES:
        raise HTTPException(status_code=403, detail="Requires a role with publish permission")


# ---- tags: API speaks lists, storage is ",a,b," (see models docstring) ----

def _join_tags(tags: list[str]) -> str:
    return f",{','.join(tags)}," if tags else ""


def _split_tags(stored: str) -> list[str]:
    return [t for t in stored.split(",") if t]


def _item_dict(item: ContentItem) -> dict:
    return {
        "content_id": item.id,
        "account_id": item.account_id,
        "created_by_user_id": item.created_by_user_id,
        "generation_job_id": item.generation_job_id,
        "title": item.title,
        "body": item.body,
        "tags": _split_tags(item.tags),
        "status": item.status,
        "version": item.version,
        "image_url": item.image_url,
        "image_asset_ref": item.image_asset_ref,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def event_payload(item: ContentItem, **extra) -> dict:
    """Common shape of every content.* event (consumer.py reuses it)."""
    return {
        "content_id": item.id,
        "account_id": item.account_id,
        "status": item.status,
        "version": item.version,
        "title": item.title,
        "generation_job_id": item.generation_job_id,
        "image_url": item.image_url,
        **extra,
    }


def snapshot_version(db: Session, item: ContentItem, user_id: str | None) -> None:
    """Append the content_versions row mirroring the item's CURRENT fields
    (call after mutating the item and bumping item.version)."""
    db.add(ContentVersion(
        content_id=item.id,
        account_id=item.account_id,
        version=item.version,
        title=item.title,
        body=item.body,
        tags=item.tags,
        image_url=item.image_url,
        image_asset_ref=item.image_asset_ref,
        edited_by_user_id=user_id,
    ))


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------

@router.post("/content", status_code=201)
def create_content(
    body: schemas.ContentCreate,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    item = ContentItem(
        account_id=claims["account_id"],
        created_by_user_id=claims.get("sub"),
        generation_job_id=body.generation_job_id,
        title=body.title,
        body=body.body,
        tags=_join_tags(body.tags),
        image_url=body.image_url,
    )
    db.add(item)
    db.flush()
    snapshot_version(db, item, claims.get("sub"))
    db.commit()
    db.refresh(item)
    events.publish("content.created", event_payload(item, created_by_user_id=claims.get("sub")))
    return _item_dict(item)


@router.get("/content")
def list_content(
    status: str | None = Query(default=None),
    tag: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    if status is not None and status not in schemas.STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {schemas.STATUSES}")
    if tag is not None and "," in tag:
        raise HTTPException(status_code=422, detail="tag must not contain commas")

    where = [ContentItem.account_id == claims["account_id"]]
    if status is not None:
        where.append(ContentItem.status == status)
    if tag is not None:
        where.append(ContentItem.tags.like(f"%,{tag},%"))

    total = db.scalar(select(func.count()).select_from(ContentItem).where(*where))
    rows = db.scalars(
        select(ContentItem).where(*where)
        .order_by(ContentItem.created_at.desc(), ContentItem.id)
        .limit(limit).offset(offset)
    ).all()
    return {"items": [_item_dict(i) for i in rows],
            "total": total, "limit": limit, "offset": offset}


@router.get("/content/{content_id}")
def get_content(
    content_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    return _item_dict(_owned(content_id, claims, db))


@router.patch("/content/{content_id}")
def update_content(
    content_id: str,
    body: schemas.ContentUpdate,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    item = _owned(content_id, claims, db)
    if item.status == "published":
        raise HTTPException(status_code=409, detail="Published content is immutable")

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update")

    if "title" in fields:
        item.title = fields["title"]
    if "body" in fields:
        item.body = fields["body"]
    if "tags" in fields:
        item.tags = _join_tags(fields["tags"])
    if "image_url" in fields:
        item.image_url = fields["image_url"]

    item.version += 1
    snapshot_version(db, item, claims.get("sub"))
    db.commit()
    db.refresh(item)
    events.publish("content.updated", event_payload(item, updated_by_user_id=claims.get("sub")))
    return _item_dict(item)


@router.delete("/content/{content_id}", status_code=204)
def delete_content(
    content_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> Response:
    item = _owned(content_id, claims, db)
    if item.status != "draft":
        # Content already in the publish pipeline: only publish roles may
        # remove it (a member deleting a scheduled post would silently break
        # the Scheduler's calendar).
        _require_publish_role(claims)

    payload = event_payload(item, deleted_by_user_id=claims.get("sub"))
    db.query(ContentVersion).filter(ContentVersion.content_id == item.id).delete()
    db.delete(item)
    db.commit()
    storage.delete_content_media(claims["account_id"], content_id)
    events.publish("content.deleted", payload)
    return Response(status_code=204)


@router.get("/content/{content_id}/versions")
def list_versions(
    content_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    item = _owned(content_id, claims, db)
    rows = db.scalars(
        select(ContentVersion).where(ContentVersion.content_id == item.id)
        .order_by(ContentVersion.version)
    ).all()
    return {"content_id": item.id, "current_version": item.version, "versions": [
        {
            "version": v.version,
            "title": v.title,
            "body": v.body,
            "tags": _split_tags(v.tags),
            "image_url": v.image_url,
            "image_asset_ref": v.image_asset_ref,
            "edited_by_user_id": v.edited_by_user_id,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        } for v in rows
    ]}


# --------------------------------------------------------------------------
# Status lifecycle
# --------------------------------------------------------------------------

@router.post("/content/{content_id}/status")
def change_status(
    content_id: str,
    body: schemas.StatusChange,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    item = _owned(content_id, claims, db)  # tenant 404 before the role 403
    _require_publish_role(claims)
    if body.status not in TRANSITIONS[item.status]:
        raise HTTPException(status_code=409, detail={
            "message": f"Cannot move {item.status} content to {body.status}",
            "status": item.status,
            "allowed": sorted(TRANSITIONS[item.status]),
        })

    previous = item.status
    item.status = body.status
    db.commit()
    db.refresh(item)
    events.publish(
        TRANSITION_EVENTS.get(body.status, "content.updated"),
        event_payload(item, previous_status=previous, changed_by_user_id=claims.get("sub")),
    )
    return _item_dict(item)


# --------------------------------------------------------------------------
# Images (manual upload path — AI-generated images arrive as image_url)
# --------------------------------------------------------------------------

@router.post("/content/{content_id}/image")
async def upload_image(
    content_id: str,
    file: UploadFile = File(...),
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    item = _owned(content_id, claims, db)
    if item.status == "published":
        raise HTTPException(status_code=409, detail="Published content is immutable")
    ext = ALLOWED_IMAGE_TYPES.get(file.content_type or "")
    if ext is None:
        raise HTTPException(status_code=415,
                            detail=f"Unsupported image type; allowed: {sorted(ALLOWED_IMAGE_TYPES)}")
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds 5 MiB limit")
    if not data:
        raise HTTPException(status_code=422, detail="Empty file")

    item.image_asset_ref = storage.save_image(item.account_id, item.id, ext, data)
    item.image_url = f"/content/{item.id}/image"
    item.version += 1  # an image change is a content change -> versioned edit
    snapshot_version(db, item, claims.get("sub"))
    db.commit()
    db.refresh(item)
    events.publish("content.updated", event_payload(item, updated_by_user_id=claims.get("sub")))
    return _item_dict(item)


@router.get("/content/{content_id}/image")
def get_image(
    content_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> FileResponse:
    item = _owned(content_id, claims, db)
    if not item.image_asset_ref:
        raise HTTPException(status_code=404, detail="No uploaded image on this content")
    path = storage.full_path(item.image_asset_ref)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Image asset missing from storage")
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)
