"""
Social Publishing endpoints — LinkedIn connections and publishing.

Authorization model (same as Scheduler/Content/AI/Usage/Credits): identity
comes from the RS256 access token, verified with the shared public key.
Everything is scoped by the token's account_id claim — cross-account access
answers 404, never 403, so ids don't leak existence.

Role model: connecting/disconnecting the account's LinkedIn and publishing
to it are account-level, outward-facing actions, so they require a publish
role (owner/admin) — the same gate the Scheduler puts on calendar mutations.
Reading connections and publish jobs is any member's business.

OAuth flow (authorization-code + CSRF state, see oauth_state.py):
  1. POST /connections/linkedin/start mints a one-time `state` bound to the
     caller's account and returns the LinkedIn consent URL; the frontend
     redirects the user there.
  2. LinkedIn redirects back to LINKEDIN_REDIRECT_URI (a FRONTEND route)
     with ?code=&state=. The frontend relays both to
     POST /connections/linkedin/callback WITH the user's bearer token — that
     keeps RS256 verification on every route of this service (a bare browser
     redirect carries no Authorization header) and lets us check the state
     was minted for THIS account. Unknown, expired, replayed, or
     other-account states are rejected before any code exchange.
  3. We exchange the code, fetch OIDC userinfo for the member URN, and store
     the connection with Fernet-encrypted tokens (crypto.py). Token values
     are never logged and never appear in any response.

Reconnecting upserts the account's existing row (one LinkedIn link per
account); DELETE is a soft disconnect that flips status AND clears the
stored ciphertext — history stays, secrets don't.

POST /publish is the manual "publish now" path: it fetches the authoritative
content record from the Content service WITH THE CALLER'S OWN TOKEN (the
same forwarding pattern the AI service uses for Usage quota checks), so
tenancy and image access work end-to-end today. Content must be approved or
scheduled — the same gate the Scheduler applies. After a successful publish
we advance the Content-side status to `published` (best-effort — Content's
docs say that transition is normally ours to make); a LinkedIn failure still
records the job, emits post.failed, and answers 502.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creditflow_common import jwt_utils

import content_client
import crypto
import database
import linkedin
import oauth_state
import publishing
import schemas
from models import PublishJob, SocialConnection, as_utc, utcnow

logger = logging.getLogger("social.routes")

router = APIRouter(tags=["social"])

# Roles that may connect/disconnect LinkedIn or publish (spec roles:
# owner / admin / member) — publishing is the outward-facing act.
PUBLISH_ROLES = ("owner", "admin")

# Content-service statuses that may be published (mirrors the Scheduler's gate).
PUBLISHABLE_CONTENT_STATUSES = ("approved", "scheduled")


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


def _require_publish_role(claims: dict) -> None:
    if claims.get("role") not in PUBLISH_ROLES:
        raise HTTPException(status_code=403, detail="Requires a role with publish permission")


def _connection_dict(row: SocialConnection) -> dict:
    """API shape of a connection — token ciphertext deliberately excluded."""
    return {
        "connection_id": row.id,
        "account_id": row.account_id,
        "provider": row.provider,
        "member_urn": row.member_urn,
        "display_name": row.display_name,
        "status": row.status,
        "scope": row.scope,
        "token_expires_at": as_utc(row.token_expires_at).isoformat() if row.token_expires_at else None,
        "connected_by_user_id": row.connected_by_user_id,
        "created_at": as_utc(row.created_at).isoformat() if row.created_at else None,
        "updated_at": as_utc(row.updated_at).isoformat() if row.updated_at else None,
    }


# --------------------------------------------------------------------------
# LinkedIn OAuth connect / callback / list / disconnect
# --------------------------------------------------------------------------

@router.post("/connections/linkedin/start")
def start_connect(claims: dict = Depends(current_claims)) -> dict:
    _require_publish_role(claims)
    try:
        state = oauth_state.issue(claims["account_id"], claims.get("sub"))
        url = linkedin.authorization_url(state)
    except linkedin.LinkedInError as exc:
        # Unconfigured app registration (each developer brings their own).
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "authorization_url": url,
        "state": state,
        "expires_in": oauth_state.STATE_TTL_SECONDS,
    }


@router.post("/connections/linkedin/callback", status_code=201)
def finish_connect(
    body: schemas.OAuthCallback,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    _require_publish_role(claims)

    # CSRF gate: the state must exist (not expired), be single-use (consume
    # deletes it), and have been minted for the caller's own account.
    stored = oauth_state.consume(body.state)
    if stored is None or stored.get("account_id") != claims["account_id"]:
        raise HTTPException(status_code=403, detail="Invalid, expired, or foreign OAuth state")

    try:
        token_data = linkedin.exchange_code(body.code)
        userinfo = linkedin.get_userinfo(token_data["access_token"])
    except linkedin.LinkedInTransientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except linkedin.LinkedInError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    now = utcnow()
    row = db.scalars(
        select(SocialConnection)
        .where(SocialConnection.account_id == claims["account_id"],
               SocialConnection.provider == "linkedin")
        .order_by(SocialConnection.created_at)
    ).first()
    if row is None:
        row = SocialConnection(account_id=claims["account_id"], provider="linkedin")
        db.add(row)

    row.member_urn = f"urn:li:person:{userinfo['sub']}"
    row.display_name = userinfo.get("name")
    row.access_token_encrypted = crypto.encrypt_token(token_data["access_token"])
    refresh = token_data.get("refresh_token")
    row.refresh_token_encrypted = crypto.encrypt_token(refresh) if refresh else None
    row.token_expires_at = now + timedelta(seconds=int(token_data.get("expires_in") or 0)) \
        if token_data.get("expires_in") else None
    row.refresh_token_expires_at = now + timedelta(seconds=int(token_data["refresh_token_expires_in"])) \
        if token_data.get("refresh_token_expires_in") else None
    row.scope = token_data.get("scope")
    row.status = "connected"
    row.connected_by_user_id = claims.get("sub")
    db.commit()
    db.refresh(row)
    return _connection_dict(row)


@router.get("/connections")
def list_connections(
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    rows = db.scalars(
        select(SocialConnection)
        .where(SocialConnection.account_id == claims["account_id"])
        .order_by(SocialConnection.created_at)
    ).all()
    return {"items": [_connection_dict(r) for r in rows]}


@router.delete("/connections/{connection_id}")
def disconnect(
    connection_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    row = db.get(SocialConnection, connection_id)
    if row is None or row.account_id != claims["account_id"]:
        raise HTTPException(status_code=404, detail="Connection not found")
    _require_publish_role(claims)
    if row.status != "connected":
        raise HTTPException(status_code=409, detail="Connection is already disconnected")
    row.status = "disconnected"
    row.access_token_encrypted = None   # secrets don't outlive the link
    row.refresh_token_encrypted = None
    row.token_expires_at = None
    row.refresh_token_expires_at = None
    db.commit()
    db.refresh(row)
    return _connection_dict(row)


# --------------------------------------------------------------------------
# Manual publish + job history
# --------------------------------------------------------------------------

@router.post("/publish", status_code=201)
def publish_now(
    body: schemas.PublishRequest,
    token: str = Depends(bearer_token),
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    _require_publish_role(claims)
    connection = publishing.active_connection(db, claims["account_id"])
    if connection is None:
        raise HTTPException(status_code=409, detail="No connected LinkedIn account")

    try:
        item = content_client.get_content(body.content_id, token)
    except content_client.ContentTransientError as exc:
        raise HTTPException(status_code=502, detail=f"Content service unavailable: {exc}")
    except content_client.ContentClientError:
        # Content tenant-scopes by the forwarded token — 401/403/404 all mean
        # "not yours / not there" and answer 404, like everything tenant-scoped.
        raise HTTPException(status_code=404, detail="Content not found")

    if item.get("status") not in PUBLISHABLE_CONTENT_STATUSES:
        raise HTTPException(status_code=409, detail={
            "message": f"Content in status {item.get('status')!r} cannot be published",
            "content_status": item.get("status"),
            "publishable": list(PUBLISHABLE_CONTENT_STATUSES),
        })

    job = publishing.run_publish(
        db,
        connection=connection,
        account_id=claims["account_id"],
        content_id=body.content_id,
        text=item.get("body") or item.get("title") or "",
        image_url=item.get("image_url"),
        image_bearer=token,          # caller's token opens Content's media route
        source="manual",
        fail_on_transient=True,      # no broker redelivery behind a REST call
    )
    db.commit()
    publishing.emit_result(job)

    if job.status != "published":
        raise HTTPException(status_code=502, detail={
            "message": "LinkedIn publish failed",
            "reason": job.error,
            "job": publishing.job_dict(job),
        })

    # Best-effort: the Content service documents `published` as normally set
    # by us; a failure here never un-publishes the LinkedIn post.
    try:
        content_client.set_status(body.content_id, "published", token)
    except content_client.ContentClientError:
        logger.warning("could not advance content %s to published", body.content_id)

    return publishing.job_dict(job)


@router.get("/publish-jobs")
def list_publish_jobs(
    status: str | None = Query(default=None),
    content_id: str | None = Query(default=None, max_length=36),
    schedule_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    if status is not None and status not in schemas.JOB_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {schemas.JOB_STATUSES}")

    where = [PublishJob.account_id == claims["account_id"]]
    if status is not None:
        where.append(PublishJob.status == status)
    if content_id is not None:
        where.append(PublishJob.content_id == content_id)
    if schedule_id is not None:
        where.append(PublishJob.schedule_id == schedule_id)

    total = db.scalar(select(func.count()).select_from(PublishJob).where(*where))
    rows = db.scalars(
        select(PublishJob).where(*where)
        .order_by(PublishJob.created_at.desc(), PublishJob.id)
        .limit(limit).offset(offset)
    ).all()
    return {"items": [publishing.job_dict(r) for r in rows],
            "total": total, "limit": limit, "offset": offset}


@router.get("/publish-jobs/{job_id}")
def get_publish_job(
    job_id: str,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    row = db.get(PublishJob, job_id)
    if row is None or row.account_id != claims["account_id"]:
        raise HTTPException(status_code=404, detail="Publish job not found")
    return publishing.job_dict(row)
