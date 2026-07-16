"""
Content service tests: every route requires a valid access token, all data is
tenant-scoped (cross-account access answers 404 and lists never leak), CRUD
is versioned (every edit appends a content_versions snapshot), the status
machine only allows the legal draft -> approved -> scheduled -> published
moves and only for publish roles, published content is immutable, the image
upload path stores and serves bytes as a versioned edit, every mutation emits
the right content.* event, and the ai.generation_completed consumer creates
post drafts idempotently (event_id replay AND fresh-event_id/same-job
replay) while skipping cancelled / non-post / empty-response events.

No infra: SQLite via conftest, consumer.handle_event called directly (the
exact function the broker would call), publisher stubbed, media in a temp
directory. No Redis anywhere — this service doesn't use it.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from creditflow_common import jwt_utils
from creditflow_common.idempotency import ProcessedEvent

import consumer
from models import ContentItem, ContentVersion

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-payload"


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(account_id: str, role: str = "member", user_id: str | None = None) -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues."""
    token, _ = jwt_utils.sign_access_token(user_id or _uid(), account_id, role)
    return {"Authorization": f"Bearer {token}"}


def _create(client, account_id: str, title: str = "Launch post", body: str = "Hello LinkedIn",
            tags: list[str] | None = None, **extra) -> dict:
    payload = {"title": title, "body": body, "tags": tags or [], **extra}
    r = client.post("/content", json=payload, headers=_auth(account_id))
    assert r.status_code == 201, r.text
    return r.json()


def _set_status(client, account_id: str, content_id: str, status: str,
                role: str = "owner") -> dict:
    r = client.post(f"/content/{content_id}/status", json={"status": status},
                    headers=_auth(account_id, role=role))
    assert r.status_code == 200, r.text
    return r.json()


def _generation_completed(account_id: str, text: str = "Big news!\nWe shipped.",
                          status: str = "completed", generation_type: str | None = "post",
                          job_id: str | None = None) -> tuple[dict, str]:
    """(payload, event_id) shaped like the AI service's ai.generation_completed
    once it adds the generation_type/response forward-contract fields."""
    payload = {
        "account_id": account_id,
        "user_id": _uid(),
        "job_id": job_id or f"job_{uuid.uuid4().hex[:12]}",
        "model": "openai/gpt-4o-mini",
        "input_tokens": 50,
        "output_tokens": 50,
        "total_tokens": 100,
        "cost_usd": 0.0025,
        "status": status,
        "usage_estimated": False,
    }
    if generation_type is not None:
        payload["generation_type"] = generation_type
    if text is not None:
        payload["response"] = text
    return payload, f"evt_{uuid.uuid4().hex}"


def _items(db, account_id: str) -> list[ContentItem]:
    db.expire_all()
    return db.scalars(select(ContentItem).where(ContentItem.account_id == account_id)).all()


def _versions(db, content_id: str) -> list[ContentVersion]:
    db.expire_all()
    return db.scalars(select(ContentVersion).where(ContentVersion.content_id == content_id)
                      .order_by(ContentVersion.version)).all()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def test_every_route_requires_auth(client):
    content_id = _uid()
    for method, path in [
        ("post", "/content"),
        ("get", "/content"),
        ("get", f"/content/{content_id}"),
        ("patch", f"/content/{content_id}"),
        ("delete", f"/content/{content_id}"),
        ("get", f"/content/{content_id}/versions"),
        ("post", f"/content/{content_id}/status"),
        ("post", f"/content/{content_id}/image"),
        ("get", f"/content/{content_id}/image"),
    ]:
        r = getattr(client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"


def test_refresh_token_is_rejected(client):
    token, _ = jwt_utils.sign_refresh_token(_uid(), _uid())
    r = client.get("/content", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


# --------------------------------------------------------------------------
# CRUD + versioning
# --------------------------------------------------------------------------

def test_create_returns_draft_v1_and_emits_created(client, published_events, db_session):
    account_id = _uid()
    item = _create(client, account_id, tags=["launch", "ai"])
    assert item["status"] == "draft"
    assert item["version"] == 1
    assert item["tags"] == ["launch", "ai"]

    versions = _versions(db_session, item["content_id"])
    assert [v.version for v in versions] == [1]

    assert published_events == [("content.created", published_events[0][1])]
    key, payload = published_events[0]
    assert key == "content.created"
    assert payload["content_id"] == item["content_id"]
    assert payload["account_id"] == account_id
    assert payload["status"] == "draft"


def test_create_links_generation_job(client):
    account_id = _uid()
    item = _create(client, account_id, generation_job_id="job_abc123")
    assert item["generation_job_id"] == "job_abc123"
    r = client.get(f"/content/{item['content_id']}", headers=_auth(account_id))
    assert r.status_code == 200
    assert r.json()["generation_job_id"] == "job_abc123"


def test_update_bumps_version_and_appends_history(client, published_events, db_session):
    account_id = _uid()
    item = _create(client, account_id, title="v1 title", body="v1 body")
    r = client.patch(f"/content/{item['content_id']}",
                     json={"title": "v2 title", "tags": ["edited"]},
                     headers=_auth(account_id))
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["version"] == 2
    assert updated["title"] == "v2 title"
    assert updated["body"] == "v1 body"  # untouched fields survive a partial update
    assert updated["tags"] == ["edited"]

    versions = _versions(db_session, item["content_id"])
    assert [(v.version, v.title) for v in versions] == [(1, "v1 title"), (2, "v2 title")]

    assert ("content.updated", published_events[-1][1]) == published_events[-1]
    assert published_events[-1][1]["version"] == 2


def test_update_with_no_fields_is_rejected(client):
    account_id = _uid()
    item = _create(client, account_id)
    r = client.patch(f"/content/{item['content_id']}", json={}, headers=_auth(account_id))
    assert r.status_code == 422


def test_versions_endpoint_lists_full_history(client):
    account_id = _uid()
    item = _create(client, account_id, title="first")
    client.patch(f"/content/{item['content_id']}", json={"body": "second body"},
                 headers=_auth(account_id))
    r = client.get(f"/content/{item['content_id']}/versions", headers=_auth(account_id))
    assert r.status_code == 200
    data = r.json()
    assert data["current_version"] == 2
    assert [v["version"] for v in data["versions"]] == [1, 2]
    assert data["versions"][0]["body"] == "Hello LinkedIn"
    assert data["versions"][1]["body"] == "second body"


def test_delete_draft_emits_deleted_and_removes_history(client, published_events, db_session):
    account_id = _uid()
    item = _create(client, account_id)
    r = client.delete(f"/content/{item['content_id']}", headers=_auth(account_id))
    assert r.status_code == 204
    assert client.get(f"/content/{item['content_id']}", headers=_auth(account_id)).status_code == 404
    assert _versions(db_session, item["content_id"]) == []
    assert published_events[-1][0] == "content.deleted"
    assert published_events[-1][1]["content_id"] == item["content_id"]


# --------------------------------------------------------------------------
# Listing: pagination + filters
# --------------------------------------------------------------------------

def test_list_paginates(client):
    account_id = _uid()
    ids = {_create(client, account_id, title=f"post {i}")["content_id"] for i in range(3)}
    r = client.get("/content?limit=2&offset=0", headers=_auth(account_id))
    assert r.status_code == 200
    page1 = r.json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    page2 = client.get("/content?limit=2&offset=2", headers=_auth(account_id)).json()
    assert len(page2["items"]) == 1
    seen = {i["content_id"] for i in page1["items"]} | {i["content_id"] for i in page2["items"]}
    assert seen == ids


def test_list_filters_by_status(client):
    account_id = _uid()
    kept = _create(client, account_id)
    approved = _create(client, account_id)
    _set_status(client, account_id, approved["content_id"], "approved")

    drafts = client.get("/content?status=draft", headers=_auth(account_id)).json()
    assert [i["content_id"] for i in drafts["items"]] == [kept["content_id"]]
    approveds = client.get("/content?status=approved", headers=_auth(account_id)).json()
    assert [i["content_id"] for i in approveds["items"]] == [approved["content_id"]]

    assert client.get("/content?status=bogus", headers=_auth(account_id)).status_code == 422


def test_list_filters_by_whole_tag(client):
    account_id = _uid()
    tagged = _create(client, account_id, tags=["ai", "launch"])
    _create(client, account_id, tags=["ai-launch"])  # substring must NOT match

    r = client.get("/content?tag=launch", headers=_auth(account_id))
    assert [i["content_id"] for i in r.json()["items"]] == [tagged["content_id"]]


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------

def test_cross_tenant_reads_are_404(client):
    item = _create(client, _uid())
    other = _auth(_uid())
    assert client.get(f"/content/{item['content_id']}", headers=other).status_code == 404
    assert client.get(f"/content/{item['content_id']}/versions", headers=other).status_code == 404


def test_cross_tenant_mutations_are_404(client, db_session):
    account_id = _uid()
    item = _create(client, account_id)
    other = _auth(_uid(), role="owner")  # even a publish role in ANOTHER account
    cid = item["content_id"]
    assert client.patch(f"/content/{cid}", json={"title": "hijack"}, headers=other).status_code == 404
    assert client.post(f"/content/{cid}/status", json={"status": "approved"}, headers=other).status_code == 404
    assert client.delete(f"/content/{cid}", headers=other).status_code == 404
    assert client.post(f"/content/{cid}/image",
                       files={"file": ("x.png", PNG_BYTES, "image/png")}, headers=other).status_code == 404
    # And the original is untouched.
    db_session.expire_all()
    assert db_session.get(ContentItem, cid).title == "Launch post"


def test_list_never_leaks_other_accounts(client):
    account_a, account_b = _uid(), _uid()
    mine = _create(client, account_a)
    _create(client, account_b)
    r = client.get("/content", headers=_auth(account_a))
    data = r.json()
    assert data["total"] == 1
    assert [i["content_id"] for i in data["items"]] == [mine["content_id"]]


# --------------------------------------------------------------------------
# Status lifecycle
# --------------------------------------------------------------------------

def test_full_lifecycle_emits_the_right_events(client, published_events):
    account_id = _uid()
    item = _create(client, account_id)
    cid = item["content_id"]

    assert _set_status(client, account_id, cid, "approved")["status"] == "approved"
    assert _set_status(client, account_id, cid, "scheduled")["status"] == "scheduled"
    assert _set_status(client, account_id, cid, "published")["status"] == "published"

    keys = [k for k, _ in published_events]
    assert keys == ["content.created", "content.approved", "content.updated", "content.published"]
    approved_payload = published_events[1][1]
    assert approved_payload["previous_status"] == "draft"
    assert approved_payload["status"] == "approved"
    scheduled_payload = published_events[2][1]
    assert scheduled_payload["previous_status"] == "approved"
    assert scheduled_payload["status"] == "scheduled"


def test_approval_can_be_revoked_and_schedule_cancelled(client):
    account_id = _uid()
    cid = _create(client, account_id)["content_id"]
    _set_status(client, account_id, cid, "approved")
    assert _set_status(client, account_id, cid, "draft")["status"] == "draft"
    _set_status(client, account_id, cid, "approved")
    _set_status(client, account_id, cid, "scheduled")
    assert _set_status(client, account_id, cid, "approved")["status"] == "approved"


def test_illegal_transitions_are_409(client):
    account_id = _uid()
    cid = _create(client, account_id)["content_id"]
    # draft cannot jump the approval gate.
    for target in ("scheduled", "published"):
        r = client.post(f"/content/{cid}/status", json={"status": target},
                        headers=_auth(account_id, role="owner"))
        assert r.status_code == 409, r.text
    # published is terminal.
    _set_status(client, account_id, cid, "approved")
    _set_status(client, account_id, cid, "published")
    for target in ("draft", "approved", "scheduled"):
        r = client.post(f"/content/{cid}/status", json={"status": target},
                        headers=_auth(account_id, role="owner"))
        assert r.status_code == 409, r.text
    assert client.post(f"/content/{cid}/status", json={"status": "bogus"},
                       headers=_auth(account_id, role="owner")).status_code == 422


def test_members_cannot_transition_but_admins_can(client):
    account_id = _uid()
    cid = _create(client, account_id)["content_id"]
    r = client.post(f"/content/{cid}/status", json={"status": "approved"},
                    headers=_auth(account_id, role="member"))
    assert r.status_code == 403
    r = client.post(f"/content/{cid}/status", json={"status": "approved"},
                    headers=_auth(account_id, role="admin"))
    assert r.status_code == 200


def test_published_content_is_immutable(client):
    account_id = _uid()
    cid = _create(client, account_id)["content_id"]
    _set_status(client, account_id, cid, "approved")
    _set_status(client, account_id, cid, "published")

    r = client.patch(f"/content/{cid}", json={"title": "rewrite history"},
                     headers=_auth(account_id, role="owner"))
    assert r.status_code == 409
    r = client.post(f"/content/{cid}/image",
                    files={"file": ("x.png", PNG_BYTES, "image/png")},
                    headers=_auth(account_id, role="owner"))
    assert r.status_code == 409


def test_non_draft_delete_requires_publish_role(client):
    account_id = _uid()
    cid = _create(client, account_id)["content_id"]
    _set_status(client, account_id, cid, "approved")
    assert client.delete(f"/content/{cid}", headers=_auth(account_id, role="member")).status_code == 403
    assert client.delete(f"/content/{cid}", headers=_auth(account_id, role="owner")).status_code == 204


# --------------------------------------------------------------------------
# Image upload
# --------------------------------------------------------------------------

def test_image_upload_stores_serves_and_versions(client, published_events):
    account_id = _uid()
    item = _create(client, account_id)
    cid = item["content_id"]
    r = client.post(f"/content/{cid}/image",
                    files={"file": ("photo.png", PNG_BYTES, "image/png")},
                    headers=_auth(account_id))
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["version"] == 2  # an image change is a versioned edit
    assert updated["image_url"] == f"/content/{cid}/image"
    assert updated["image_asset_ref"]

    r = client.get(f"/content/{cid}/image", headers=_auth(account_id))
    assert r.status_code == 200
    assert r.content == PNG_BYTES
    assert r.headers["content-type"] == "image/png"
    assert published_events[-1][0] == "content.updated"


def test_image_upload_rejects_non_image(client):
    account_id = _uid()
    cid = _create(client, account_id)["content_id"]
    r = client.post(f"/content/{cid}/image",
                    files={"file": ("evil.exe", b"MZ...", "application/octet-stream")},
                    headers=_auth(account_id))
    assert r.status_code == 415


def test_image_fetch_without_upload_is_404(client):
    account_id = _uid()
    cid = _create(client, account_id, image_url="https://cdn.example.com/ext.png")["content_id"]
    # An EXTERNAL image_url is not a stored asset — nothing to serve.
    assert client.get(f"/content/{cid}/image", headers=_auth(account_id)).status_code == 404


# --------------------------------------------------------------------------
# ai.generation_completed consumer
# --------------------------------------------------------------------------

def test_consumer_creates_post_draft(client, published_events, db_session):
    account_id = _uid()
    payload, event_id = _generation_completed(account_id, text="Big news!\nWe shipped v2.")
    consumer.handle_event("ai.generation_completed", payload, event_id)

    items = _items(db_session, account_id)
    assert len(items) == 1
    item = items[0]
    assert item.status == "draft"
    assert item.version == 1
    assert item.body == "Big news!\nWe shipped v2."
    assert item.title == "Big news!"  # first line becomes the working title
    assert item.generation_job_id == payload["job_id"]
    assert item.created_by_user_id == payload["user_id"]
    assert [v.version for v in _versions(db_session, item.id)] == [1]

    assert published_events == [("content.created", published_events[0][1])]
    assert published_events[0][1]["content_id"] == item.id
    assert published_events[0][1]["account_id"] == account_id

    # And the draft is visible through the tenant-scoped API.
    r = client.get(f"/content/{item.id}", headers=_auth(account_id))
    assert r.status_code == 200


def test_consumer_is_idempotent_on_event_replay(client, published_events, db_session):
    account_id = _uid()
    payload, event_id = _generation_completed(account_id)
    consumer.handle_event("ai.generation_completed", payload, event_id)
    consumer.handle_event("ai.generation_completed", payload, event_id)  # broker redelivery
    assert len(_items(db_session, account_id)) == 1
    assert len(published_events) == 1
    assert db_session.get(ProcessedEvent, event_id) is not None


def test_consumer_is_idempotent_on_same_job_fresh_event_id(client, published_events, db_session):
    account_id = _uid()
    payload, event_id = _generation_completed(account_id)
    consumer.handle_event("ai.generation_completed", payload, event_id)
    # AI re-emits the same generation under a brand-new event_id.
    consumer.handle_event("ai.generation_completed", payload, f"evt_{uuid.uuid4().hex}")
    assert len(_items(db_session, account_id)) == 1
    assert len(published_events) == 1


def test_consumer_skips_non_draft_material(client, published_events, db_session):
    account_id = _uid()
    # Cancelled generation (metered by Usage, but not draft material).
    payload, event_id = _generation_completed(account_id, status="cancelled")
    consumer.handle_event("ai.generation_completed", payload, event_id)
    # Not a 'post' generation — including today's AI payload with NO type field.
    payload, event_id = _generation_completed(account_id, generation_type=None)
    consumer.handle_event("ai.generation_completed", payload, event_id)
    # Post-typed but no text to draft.
    payload, event_id = _generation_completed(account_id, text="   ")
    consumer.handle_event("ai.generation_completed", payload, event_id)

    assert _items(db_session, account_id) == []
    assert published_events == []
    # Every event was still recorded as processed (skips are permanent).
    assert db_session.get(ProcessedEvent, event_id) is not None
