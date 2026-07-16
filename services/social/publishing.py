"""
The publish pipeline shared by the manual route (POST /publish) and the
content.scheduled consumer — decrypt the connection's token, run the image
flow when there is an image, create the UGC post, and record a terminal
publish_jobs row. Callers commit and then emit_result — the repo's
commit-first-then-publish rule, so an event is never announced for a row
that didn't land.

Image flow (the spec's REQUIRED bonus), exactly LinkedIn's three steps:
  1. register the upload (assets?action=registerUpload) -> uploadUrl + asset URN
  2. PUT the binary to uploadUrl
  3. create the post with shareMediaCategory=IMAGE referencing the asset URN
The bytes come from content_client.get_image (Content-service stored media
via its /content/{id}/image route, or the absolute URL of an AI-generated
image). A post_media row records the source -> asset URN mapping.

Failure semantics:
  - PERMANENT errors (LinkedIn 4xx, image 4xx, bad state) conclude the
    attempt: the job row is written status=failed with the reason, and
    emit_result announces post.failed.
  - TRANSIENT errors (network, 429, 5xx) propagate by default so the
    consumer's broker redelivery (bounded retry -> DLQ, per spec) can try
    again with NOTHING committed. The manual route has no redelivery, so it
    passes fail_on_transient=True and the transient error concludes the job
    like a permanent one.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

import content_client
import crypto
import events
import linkedin
from models import PostMedia, PublishJob, SocialConnection, as_utc, new_uuid

logger = logging.getLogger("social.publishing")


def active_connection(db: Session, account_id: str,
                      provider: str = "linkedin") -> SocialConnection | None:
    """The account's connected LinkedIn link (newest first, if several)."""
    return db.scalars(
        select(SocialConnection)
        .where(SocialConnection.account_id == account_id,
               SocialConnection.provider == provider,
               SocialConnection.status == "connected")
        .order_by(SocialConnection.updated_at.desc())
    ).first()


def run_publish(db: Session, *, connection: SocialConnection, account_id: str,
                content_id: str, text: str, image_url: str | None,
                image_bearer: str | None, source: str, schedule_id: str | None = None,
                event_id: str | None = None, text_source: str = "content",
                fail_on_transient: bool = False) -> PublishJob:
    """Run the LinkedIn publish and add the terminal PublishJob (and
    PostMedia) rows to `db` — NOT committed here; the caller owns the
    transaction. Transient errors propagate unless fail_on_transient."""
    job = PublishJob(
        id=new_uuid(),
        account_id=account_id,
        content_id=content_id,
        schedule_id=schedule_id,
        event_id=event_id,
        connection_id=connection.id,
        source=source,
        status="failed",
        text=text or "",
        text_source=text_source,
        image_included=bool(image_url),
    )
    try:
        if not (text or "").strip() and not image_url:
            raise linkedin.LinkedInError("nothing to publish: content has neither text nor image")
        access_token = crypto.decrypt_token(connection.access_token_encrypted or "")

        asset_urn = None
        if image_url:
            data, content_type = content_client.get_image(image_url, image_bearer)
            upload = linkedin.register_image_upload(access_token, connection.member_urn)
            linkedin.upload_image_binary(access_token, upload["upload_url"], data, content_type)
            asset_urn = upload["asset_urn"]
            db.add(PostMedia(
                publish_job_id=job.id,
                account_id=account_id,
                source_url=image_url,
                linkedin_asset_urn=asset_urn,
                content_type=content_type,
                size_bytes=len(data),
            ))

        post = linkedin.create_post(access_token, connection.member_urn, text, asset_urn=asset_urn)
        job.status = "published"
        job.linkedin_post_id = post["post_id"]
        job.linkedin_post_url = post["post_url"]
    except (linkedin.LinkedInTransientError, content_client.ContentTransientError) as exc:
        if not fail_on_transient:
            raise  # consumer path: let the broker redeliver; commit nothing
        job.error = str(exc)[:1000]
    except (linkedin.LinkedInError, content_client.ContentClientError, crypto.CryptoError) as exc:
        job.error = str(exc)[:1000]

    db.add(job)
    return job


def failed_job(*, account_id: str, content_id: str, reason: str, source: str,
               schedule_id: str | None = None, event_id: str | None = None,
               connection_id: str | None = None, text: str = "",
               text_source: str = "content", image_included: bool = False) -> PublishJob:
    """A terminal failed job for attempts that never reached LinkedIn
    (no connection, content gone). Caller adds/commits and emit_result-s."""
    return PublishJob(
        id=new_uuid(),
        account_id=account_id,
        content_id=content_id,
        schedule_id=schedule_id,
        event_id=event_id,
        connection_id=connection_id,
        source=source,
        status="failed",
        text=text or "",
        text_source=text_source,
        image_included=image_included,
        error=reason[:1000],
    )


def job_dict(job: PublishJob) -> dict:
    return {
        "job_id": job.id,
        "account_id": job.account_id,
        "content_id": job.content_id,
        "schedule_id": job.schedule_id,
        "connection_id": job.connection_id,
        "source": job.source,
        "status": job.status,
        "text": job.text,
        "text_source": job.text_source,
        "image_included": job.image_included,
        "linkedin_post_id": job.linkedin_post_id,
        "linkedin_post_url": job.linkedin_post_url,
        "error": job.error,
        "created_at": as_utc(job.created_at).isoformat() if job.created_at else None,
        "updated_at": as_utc(job.updated_at).isoformat() if job.updated_at else None,
    }


def emit_result(job: PublishJob) -> None:
    """Announce the committed outcome — post.published or post.failed (see
    events.py for the payload contract)."""
    payload = {
        "job_id": job.id,
        "account_id": job.account_id,
        "content_id": job.content_id,
        "schedule_id": job.schedule_id,
        "connection_id": job.connection_id,
        "source": job.source,
        "text_source": job.text_source,
        "image_included": job.image_included,
        "linkedin_post_id": job.linkedin_post_id,
        "linkedin_post_url": job.linkedin_post_url,
        "error": job.error,
        "fire_event_id": job.event_id,
    }
    key = events.PUBLISHED_KEY if job.status == "published" else events.FAILED_KEY
    events.publish(key, payload)
