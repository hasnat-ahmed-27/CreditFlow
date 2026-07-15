"""
Social Publishing service tests: every route requires a valid access token,
all data is tenant-scoped (cross-account access answers 404 and lists never
leak), connect/disconnect/publish need a publish role while any member can
read, the OAuth flow round-trips (start mints a one-time CSRF state, the
callback exchanges the code and stores Fernet-ENCRYPTED tokens that never
appear in any response) and rejects unknown/replayed/foreign states, manual
publishing forwards the caller's own bearer to the Content service and runs
the full image pipeline (register upload -> PUT binary -> asset URN in the
UGC post), and the content.scheduled consumer publishes exactly once per
fire event (processed_events idempotency: a redelivered event never
double-posts), retries transient LinkedIn failures via raise-without-commit,
and concludes permanent failures as post.failed.

No infra: SQLite via conftest, OAuth state on fakeredis, publisher stubbed,
every linkedin.py / content_client.py function faked (base URLs also point
at a dead address so nothing can reach the network), and
consumer.handle_event called directly (the exact function the broker would).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from creditflow_common import jwt_utils
from creditflow_common.idempotency import ProcessedEvent

import consumer
import content_client
import crypto
import linkedin
from conftest import (
    LI_ACCESS_TOKEN,
    LI_ASSET_URN,
    LI_MEMBER_SUB,
    LI_POST_ID,
    LI_REFRESH_TOKEN,
)
from models import PostMedia, PublishJob, SocialConnection, utcnow


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(account_id: str, role: str = "owner", user_id: str | None = None) -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues.
    Default role is owner: connect/disconnect/publish require a publish role."""
    token, _ = jwt_utils.sign_access_token(user_id or _uid(), account_id, role)
    return {"Authorization": f"Bearer {token}"}


def _connect(client, account_id: str, role: str = "owner") -> dict:
    """Run the full OAuth start -> callback flow through the API; returns the
    connection dict."""
    headers = _auth(account_id, role=role)
    start = client.post("/connections/linkedin/start", headers=headers)
    assert start.status_code == 200, start.text
    r = client.post("/connections/linkedin/callback",
                    json={"code": "auth-code-1", "state": start.json()["state"]},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _seed_content(content_api, account_id: str, status: str = "approved",
                  body: str = "The post body.", title: str = "Launch post",
                  image_url: str | None = None) -> str:
    """Register an item in the fake Content service; returns content_id."""
    content_id = _uid()
    content_api["items"][content_id] = {
        "content_id": content_id,
        "account_id": account_id,
        "title": title,
        "body": body,
        "status": status,
        "version": 1,
        "image_url": image_url,
        "image_asset_ref": None,
    }
    return content_id


def _fire_event(account_id: str, content_id: str | None = None,
                title: str = "Scheduled launch post",
                image_url: str | None = None) -> tuple[dict, str]:
    """(payload, event_id) shaped like the Scheduler's fire task emits."""
    payload = {
        "schedule_id": _uid(),
        "account_id": account_id,
        "content_id": content_id or _uid(),
        "publish_at": utcnow().isoformat(),
        "fired_at": utcnow().isoformat(),
        "timezone": "UTC",
        "recurrence": None,
        "fire_count": 1,
        "title": title,
        "image_url": image_url,
        "created_by_user_id": _uid(),
    }
    return payload, f"evt_{uuid.uuid4().hex}"


def _jobs(db, account_id: str) -> list[PublishJob]:
    db.expire_all()
    return db.scalars(select(PublishJob).where(PublishJob.account_id == account_id)).all()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def test_every_route_requires_auth(client):
    cid = _uid()
    for method, path in [
        ("post", "/connections/linkedin/start"),
        ("post", "/connections/linkedin/callback"),
        ("get", "/connections"),
        ("delete", f"/connections/{cid}"),
        ("post", "/publish"),
        ("get", "/publish-jobs"),
        ("get", f"/publish-jobs/{cid}"),
    ]:
        r = getattr(client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"


def test_refresh_token_is_rejected(client):
    token, _ = jwt_utils.sign_refresh_token(_uid(), _uid())
    r = client.get("/connections", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_garbage_token_is_rejected(client):
    r = client.get("/connections", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


# --------------------------------------------------------------------------
# OAuth connect flow
# --------------------------------------------------------------------------

def test_start_returns_authorization_url_and_stores_state(client, fake_redis):
    r = client.post("/connections/linkedin/start", headers=_auth(_uid()))
    assert r.status_code == 200, r.text
    data = r.json()
    url, state = data["authorization_url"], data["state"]
    assert "client_id=test-client-id" in url
    assert "w_member_social" in url
    assert state in url
    # The CSRF state is parked in Redis with a TTL until the callback consumes it.
    assert fake_redis.ttl(f"social:oauth_state:{state}") > 0


def test_start_requires_publish_role(client):
    r = client.post("/connections/linkedin/start", headers=_auth(_uid(), role="member"))
    assert r.status_code == 403


def test_callback_happy_path_connects_account(client, linkedin_api):
    account_id = _uid()
    conn = _connect(client, account_id)
    assert conn["status"] == "connected"
    assert conn["member_urn"] == f"urn:li:person:{LI_MEMBER_SUB}"
    assert conn["display_name"] == "Test Member"
    assert linkedin_api["exchange_calls"] == ["auth-code-1"]
    assert linkedin_api["userinfo_calls"] == [LI_ACCESS_TOKEN]


def test_tokens_are_encrypted_at_rest_and_never_in_responses(client, db_session):
    account_id = _uid()
    headers = _auth(account_id)
    start = client.post("/connections/linkedin/start", headers=headers)
    r = client.post("/connections/linkedin/callback",
                    json={"code": "auth-code-1", "state": start.json()["state"]},
                    headers=headers)
    assert r.status_code == 201
    # Never in any API response...
    assert LI_ACCESS_TOKEN not in r.text and LI_REFRESH_TOKEN not in r.text
    listed = client.get("/connections", headers=headers)
    assert LI_ACCESS_TOKEN not in listed.text and LI_REFRESH_TOKEN not in listed.text
    # ...and only ciphertext in the database, which round-trips through Fernet.
    row = db_session.scalars(select(SocialConnection)
                             .where(SocialConnection.account_id == account_id)).one()
    assert LI_ACCESS_TOKEN not in (row.access_token_encrypted or "")
    assert crypto.decrypt_token(row.access_token_encrypted) == LI_ACCESS_TOKEN
    assert crypto.decrypt_token(row.refresh_token_encrypted) == LI_REFRESH_TOKEN
    assert row.token_expires_at is not None


def test_callback_rejects_unknown_state(client, linkedin_api, db_session):
    r = client.post("/connections/linkedin/callback",
                    json={"code": "auth-code-1", "state": "forged-state"},
                    headers=_auth(_uid()))
    assert r.status_code == 403
    assert linkedin_api["exchange_calls"] == []  # rejected BEFORE any code exchange
    assert db_session.scalars(select(SocialConnection)).all() == []


def test_callback_rejects_state_minted_for_another_account(client, linkedin_api):
    victim, attacker = _uid(), _uid()
    start = client.post("/connections/linkedin/start", headers=_auth(victim))
    r = client.post("/connections/linkedin/callback",
                    json={"code": "attacker-code", "state": start.json()["state"]},
                    headers=_auth(attacker))
    assert r.status_code == 403
    assert linkedin_api["exchange_calls"] == []


def test_callback_state_is_single_use(client, linkedin_api):
    account_id = _uid()
    headers = _auth(account_id)
    start = client.post("/connections/linkedin/start", headers=headers)
    state = start.json()["state"]
    first = client.post("/connections/linkedin/callback",
                        json={"code": "auth-code-1", "state": state}, headers=headers)
    assert first.status_code == 201
    replay = client.post("/connections/linkedin/callback",
                         json={"code": "auth-code-2", "state": state}, headers=headers)
    assert replay.status_code == 403
    assert linkedin_api["exchange_calls"] == ["auth-code-1"]


def test_reconnect_reuses_the_account_row(client, db_session):
    account_id = _uid()
    first = _connect(client, account_id)
    second = _connect(client, account_id)
    assert first["connection_id"] == second["connection_id"]
    rows = db_session.scalars(select(SocialConnection)
                              .where(SocialConnection.account_id == account_id)).all()
    assert len(rows) == 1 and rows[0].status == "connected"


def test_callback_maps_linkedin_failure(client, linkedin_api):
    headers = _auth(_uid())
    start = client.post("/connections/linkedin/start", headers=headers)
    linkedin_api["errors"]["exchange_code"] = linkedin.LinkedInError(
        "token exchange: LinkedIn HTTP 400: invalid code", status_code=400)
    r = client.post("/connections/linkedin/callback",
                    json={"code": "bad-code", "state": start.json()["state"]},
                    headers=headers)
    assert r.status_code == 400


# --------------------------------------------------------------------------
# Connections: list / disconnect / tenancy
# --------------------------------------------------------------------------

def test_list_connections_is_tenant_scoped(client):
    account_a, account_b = _uid(), _uid()
    _connect(client, account_a)
    r = client.get("/connections", headers=_auth(account_b))
    assert r.json()["items"] == []
    r = client.get("/connections", headers=_auth(account_a, role="member"))  # members may read
    assert len(r.json()["items"]) == 1


def test_disconnect_clears_tokens_and_is_terminal(client, db_session):
    account_id = _uid()
    conn = _connect(client, account_id)
    r = client.delete(f"/connections/{conn['connection_id']}", headers=_auth(account_id))
    assert r.status_code == 200 and r.json()["status"] == "disconnected"
    row = db_session.get(SocialConnection, conn["connection_id"])
    db_session.refresh(row)
    assert row.access_token_encrypted is None and row.refresh_token_encrypted is None
    again = client.delete(f"/connections/{conn['connection_id']}", headers=_auth(account_id))
    assert again.status_code == 409


def test_disconnect_cross_account_is_404_and_member_is_403(client):
    account_id = _uid()
    conn = _connect(client, account_id)
    r = client.delete(f"/connections/{conn['connection_id']}", headers=_auth(_uid()))
    assert r.status_code == 404  # existence never leaks across tenants
    r = client.delete(f"/connections/{conn['connection_id']}",
                      headers=_auth(account_id, role="member"))
    assert r.status_code == 403


# --------------------------------------------------------------------------
# Manual publish — text
# --------------------------------------------------------------------------

def test_publish_text_post(client, db_session, content_api, linkedin_api, published_events):
    account_id = _uid()
    _connect(client, account_id)
    content_id = _seed_content(content_api, account_id, body="Hello LinkedIn!")

    r = client.post("/publish", json={"content_id": content_id}, headers=_auth(account_id))
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "published"
    assert job["linkedin_post_id"] == LI_POST_ID
    assert job["source"] == "manual"

    # Exactly one UGC post, text from the authoritative body, no media.
    assert len(linkedin_api["create_calls"]) == 1
    create = linkedin_api["create_calls"][0]
    assert create["text"] == "Hello LinkedIn!"
    assert create["asset_urn"] is None
    assert create["author_urn"] == f"urn:li:person:{LI_MEMBER_SUB}"
    assert create["access_token"] == LI_ACCESS_TOKEN  # decrypted only for the call

    # post.published carries the LinkedIn record for Notification/Admin.
    published = [(k, p) for k, p in published_events if k == "post.published"]
    assert len(published) == 1
    assert published[0][1]["linkedin_post_id"] == LI_POST_ID
    assert published[0][1]["content_id"] == content_id

    assert len(_jobs(db_session, account_id)) == 1


def test_publish_forwards_callers_bearer_to_content(client, content_api):
    account_id = _uid()
    _connect(client, account_id)
    content_id = _seed_content(content_api, account_id)
    headers = _auth(account_id)
    r = client.post("/publish", json={"content_id": content_id}, headers=headers)
    assert r.status_code == 201
    caller_token = headers["Authorization"].split(" ", 1)[1]
    assert content_api["get_calls"][0]["bearer_token"] == caller_token


def test_publish_advances_content_status_best_effort(client, content_api):
    account_id = _uid()
    _connect(client, account_id)
    content_id = _seed_content(content_api, account_id)
    r = client.post("/publish", json={"content_id": content_id}, headers=_auth(account_id))
    assert r.status_code == 201
    assert len(content_api["status_calls"]) == 1
    assert content_api["status_calls"][0]["content_id"] == content_id
    assert content_api["status_calls"][0]["status"] == "published"


def test_publish_requires_connection(client, content_api, linkedin_api):
    account_id = _uid()
    content_id = _seed_content(content_api, account_id)
    r = client.post("/publish", json={"content_id": content_id}, headers=_auth(account_id))
    assert r.status_code == 409
    assert linkedin_api["create_calls"] == []


def test_publish_requires_publish_role(client, content_api):
    account_id = _uid()
    _connect(client, account_id)
    content_id = _seed_content(content_api, account_id)
    r = client.post("/publish", json={"content_id": content_id},
                    headers=_auth(account_id, role="member"))
    assert r.status_code == 403


def test_publish_unknown_content_is_404(client):
    account_id = _uid()
    _connect(client, account_id)
    r = client.post("/publish", json={"content_id": _uid()}, headers=_auth(account_id))
    assert r.status_code == 404


def test_publish_rejects_unapproved_content(client, content_api, linkedin_api):
    account_id = _uid()
    _connect(client, account_id)
    content_id = _seed_content(content_api, account_id, status="draft")
    r = client.post("/publish", json={"content_id": content_id}, headers=_auth(account_id))
    assert r.status_code == 409
    assert linkedin_api["create_calls"] == []


def test_publish_linkedin_failure_records_job_and_emits_post_failed(
        client, db_session, content_api, linkedin_api, published_events):
    account_id = _uid()
    _connect(client, account_id)
    content_id = _seed_content(content_api, account_id)
    linkedin_api["errors"]["create_post"] = linkedin.LinkedInError(
        "create post: LinkedIn HTTP 422: unprocessable", status_code=422)

    r = client.post("/publish", json={"content_id": content_id}, headers=_auth(account_id))
    assert r.status_code == 502
    jobs = _jobs(db_session, account_id)
    assert len(jobs) == 1 and jobs[0].status == "failed"
    assert "422" in jobs[0].error
    failed = [(k, p) for k, p in published_events if k == "post.failed"]
    assert len(failed) == 1 and failed[0][1]["error"] == jobs[0].error
    # The Content-side status is NOT advanced for a failed publish.
    assert content_api["status_calls"] == []


# --------------------------------------------------------------------------
# Manual publish — the image bonus (register -> upload -> create-post)
# --------------------------------------------------------------------------

def test_publish_image_post_runs_full_media_flow(
        client, db_session, content_api, linkedin_api, published_events):
    account_id = _uid()
    _connect(client, account_id)
    content_id = _seed_content(content_api, account_id, body="Post with picture",
                               image_url=f"/content/{_uid()}/image")

    r = client.post("/publish", json={"content_id": content_id}, headers=_auth(account_id))
    assert r.status_code == 201, r.text
    assert r.json()["image_included"] is True

    # Step 0: bytes pulled from the Content service's stored media, with the
    # caller's bearer (the image route is authed).
    assert len(content_api["image_calls"]) == 1
    assert content_api["image_calls"][0]["bearer_token"] is not None
    # Step 1: register the upload for the connected member.
    assert len(linkedin_api["register_calls"]) == 1
    assert linkedin_api["register_calls"][0]["owner_urn"] == f"urn:li:person:{LI_MEMBER_SUB}"
    # Step 2: the binary goes to the uploadUrl the register call returned.
    assert len(linkedin_api["upload_calls"]) == 1
    upload = linkedin_api["upload_calls"][0]
    assert upload["upload_url"] == linkedin_api["register_result"]["upload_url"]
    assert upload["data"] == content_api["image_result"][0]
    # Step 3: the UGC post references the returned asset URN.
    assert linkedin_api["create_calls"][0]["asset_urn"] == LI_ASSET_URN

    # post_media records the source -> asset mapping.
    media = db_session.scalars(select(PostMedia)
                               .where(PostMedia.account_id == account_id)).all()
    assert len(media) == 1
    assert media[0].linkedin_asset_urn == LI_ASSET_URN
    assert media[0].publish_job_id == r.json()["job_id"]

    published = [(k, p) for k, p in published_events if k == "post.published"]
    assert published[0][1]["image_included"] is True


def test_publish_image_upload_failure_fails_the_job(client, content_api, linkedin_api):
    account_id = _uid()
    _connect(client, account_id)
    content_id = _seed_content(content_api, account_id, image_url="https://img.example/x.png")
    linkedin_api["errors"]["upload_image_binary"] = linkedin.LinkedInError(
        "image upload: LinkedIn HTTP 400: bad media", status_code=400)
    r = client.post("/publish", json={"content_id": content_id}, headers=_auth(account_id))
    assert r.status_code == 502
    assert linkedin_api["create_calls"] == []  # never got to the post


# --------------------------------------------------------------------------
# Publish-job history
# --------------------------------------------------------------------------

def test_publish_jobs_listing_filters_and_tenancy(client, content_api):
    account_a, account_b = _uid(), _uid()
    _connect(client, account_a)
    cid = _seed_content(content_api, account_a)
    client.post("/publish", json={"content_id": cid}, headers=_auth(account_a))

    r = client.get("/publish-jobs", headers=_auth(account_a, role="member"))  # members may read
    assert r.json()["total"] == 1
    job_id = r.json()["items"][0]["job_id"]
    assert client.get("/publish-jobs", params={"status": "failed"},
                      headers=_auth(account_a)).json()["total"] == 0
    assert client.get("/publish-jobs", params={"content_id": cid},
                      headers=_auth(account_a)).json()["total"] == 1
    assert client.get("/publish-jobs", params={"status": "bogus"},
                      headers=_auth(account_a)).status_code == 422

    # Tenancy: the other account sees nothing, by list or by id.
    assert client.get("/publish-jobs", headers=_auth(account_b)).json()["total"] == 0
    assert client.get(f"/publish-jobs/{job_id}", headers=_auth(account_b)).status_code == 404
    assert client.get(f"/publish-jobs/{job_id}", headers=_auth(account_a)).status_code == 200


# --------------------------------------------------------------------------
# Consumer: content.scheduled -> LinkedIn
# --------------------------------------------------------------------------

def test_consumer_publishes_scheduled_post(client, db_session, linkedin_api, published_events):
    account_id = _uid()
    _connect(client, account_id)
    payload, event_id = _fire_event(account_id)
    consumer.handle_event("content.scheduled", payload, event_id)

    assert len(linkedin_api["create_calls"]) == 1
    # No SOCIAL_CONTENT_TOKEN in tests -> the fire payload's title mirror.
    assert linkedin_api["create_calls"][0]["text"] == payload["title"]

    jobs = _jobs(db_session, account_id)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == "published" and job.source == "scheduled"
    assert job.schedule_id == payload["schedule_id"]
    assert job.event_id == event_id
    assert job.text_source == "event"

    published = [(k, p) for k, p in published_events if k == "post.published"]
    assert len(published) == 1
    assert published[0][1]["linkedin_post_id"] == LI_POST_ID
    assert published[0][1]["schedule_id"] == payload["schedule_id"]
    assert published[0][1]["fire_event_id"] == event_id


def test_consumer_redelivered_event_never_double_posts(client, db_session,
                                                       linkedin_api, published_events):
    account_id = _uid()
    _connect(client, account_id)
    payload, event_id = _fire_event(account_id)
    consumer.handle_event("content.scheduled", payload, event_id)
    consumer.handle_event("content.scheduled", payload, event_id)  # broker redelivery

    assert len(linkedin_api["create_calls"]) == 1
    assert len(_jobs(db_session, account_id)) == 1
    assert len([k for k, _ in published_events if k == "post.published"]) == 1


def test_consumer_image_flow_from_fire_payload(client, db_session, content_api, linkedin_api):
    account_id = _uid()
    _connect(client, account_id)
    payload, event_id = _fire_event(account_id, image_url="https://images.example/ai-gen.png")
    consumer.handle_event("content.scheduled", payload, event_id)

    # Absolute URL (AI-generated image): fetched directly, no bearer to leak.
    assert content_api["image_calls"][0]["image_url"] == "https://images.example/ai-gen.png"
    assert content_api["image_calls"][0]["bearer_token"] is None
    assert len(linkedin_api["register_calls"]) == 1
    assert len(linkedin_api["upload_calls"]) == 1
    assert linkedin_api["create_calls"][0]["asset_urn"] == LI_ASSET_URN
    media = db_session.scalars(select(PostMedia)
                               .where(PostMedia.account_id == account_id)).all()
    assert len(media) == 1 and media[0].linkedin_asset_urn == LI_ASSET_URN


def test_consumer_without_connection_emits_post_failed(client, db_session,
                                                       linkedin_api, published_events):
    account_id = _uid()  # never connected
    payload, event_id = _fire_event(account_id)
    consumer.handle_event("content.scheduled", payload, event_id)

    assert linkedin_api["create_calls"] == []
    jobs = _jobs(db_session, account_id)
    assert len(jobs) == 1 and jobs[0].status == "failed"
    assert "no connected LinkedIn account" in jobs[0].error
    failed = [(k, p) for k, p in published_events if k == "post.failed"]
    assert len(failed) == 1 and failed[0][1]["schedule_id"] == payload["schedule_id"]
    # Concluded: a redelivery is a no-op, not a second failure event.
    consumer.handle_event("content.scheduled", payload, event_id)
    assert len(_jobs(db_session, account_id)) == 1


def test_consumer_permanent_linkedin_error_concludes_as_failed(
        client, db_session, linkedin_api, published_events):
    account_id = _uid()
    _connect(client, account_id)
    linkedin_api["errors"]["create_post"] = linkedin.LinkedInError(
        "create post: LinkedIn HTTP 401: revoked", status_code=401)
    payload, event_id = _fire_event(account_id)
    consumer.handle_event("content.scheduled", payload, event_id)

    jobs = _jobs(db_session, account_id)
    assert len(jobs) == 1 and jobs[0].status == "failed" and "401" in jobs[0].error
    assert len([k for k, _ in published_events if k == "post.failed"]) == 1
    assert db_session.get(ProcessedEvent, event_id) is not None  # concluded, not retried


def test_consumer_transient_error_retries_then_posts_exactly_once(
        client, db_session, linkedin_api, published_events):
    """Spec: retry with backoff on transient failures. The handler raises
    with NOTHING committed (no job, event not marked processed, no event
    emitted) so the shared consumer redelivers; the retry then posts once."""
    account_id = _uid()
    _connect(client, account_id)
    payload, event_id = _fire_event(account_id)

    linkedin_api["errors"]["create_post"] = linkedin.LinkedInTransientError(
        "create post: LinkedIn HTTP 503: down", status_code=503)
    with pytest.raises(linkedin.LinkedInTransientError):
        consumer.handle_event("content.scheduled", payload, event_id)

    assert _jobs(db_session, account_id) == []
    assert db_session.get(ProcessedEvent, event_id) is None
    assert published_events == []

    linkedin_api["errors"].clear()  # LinkedIn recovered; broker redelivers
    consumer.handle_event("content.scheduled", payload, event_id)
    assert len(linkedin_api["create_calls"]) == 2  # first attempt + successful retry
    jobs = _jobs(db_session, account_id)
    assert len(jobs) == 1 and jobs[0].status == "published"
    assert len([k for k, _ in published_events if k == "post.published"]) == 1


def test_consumer_uses_authoritative_body_with_service_token(
        client, db_session, monkeypatch, content_api, linkedin_api):
    monkeypatch.setenv("SOCIAL_CONTENT_TOKEN", "svc-dev-token")
    account_id = _uid()
    _connect(client, account_id)
    content_id = _seed_content(content_api, account_id, body="Authoritative body text")
    payload, event_id = _fire_event(account_id, content_id=content_id, title="Mirror title")
    consumer.handle_event("content.scheduled", payload, event_id)

    assert content_api["get_calls"][0]["bearer_token"] == "svc-dev-token"
    assert linkedin_api["create_calls"][0]["text"] == "Authoritative body text"
    assert _jobs(db_session, account_id)[0].text_source == "content"


def test_consumer_falls_back_to_mirrors_when_service_token_rejected(
        client, monkeypatch, content_api, linkedin_api):
    monkeypatch.setenv("SOCIAL_CONTENT_TOKEN", "svc-dev-token")
    content_api["errors"]["get_content"] = content_client.ContentClientError(
        "content: HTTP 401", status_code=401)
    account_id = _uid()
    _connect(client, account_id)
    payload, event_id = _fire_event(account_id, title="Mirror title")
    consumer.handle_event("content.scheduled", payload, event_id)
    assert linkedin_api["create_calls"][0]["text"] == "Mirror title"


def test_consumer_content_gone_fails_the_job(client, db_session, monkeypatch,
                                             content_api, linkedin_api, published_events):
    monkeypatch.setenv("SOCIAL_CONTENT_TOKEN", "svc-dev-token")
    account_id = _uid()
    _connect(client, account_id)
    payload, event_id = _fire_event(account_id)  # content_id not in the fake -> 404
    consumer.handle_event("content.scheduled", payload, event_id)

    assert linkedin_api["create_calls"] == []
    jobs = _jobs(db_session, account_id)
    assert len(jobs) == 1 and jobs[0].status == "failed"
    assert len([k for k, _ in published_events if k == "post.failed"]) == 1


def test_consumer_drops_malformed_event_once(client, db_session, linkedin_api):
    payload = {"schedule_id": _uid()}  # no account_id / content_id
    event_id = f"evt_{uuid.uuid4().hex}"
    consumer.handle_event("content.scheduled", payload, event_id)
    assert linkedin_api["create_calls"] == []
    assert db_session.get(ProcessedEvent, event_id) is not None  # dropped forever


def test_consumer_empty_content_fails_honestly(client, db_session, published_events):
    account_id = _uid()
    _connect(client, account_id)
    payload, event_id = _fire_event(account_id, title="", image_url=None)
    consumer.handle_event("content.scheduled", payload, event_id)
    jobs = _jobs(db_session, account_id)
    assert len(jobs) == 1 and jobs[0].status == "failed"
    assert "neither text nor image" in jobs[0].error
    assert len([k for k, _ in published_events if k == "post.failed"]) == 1
