"""
Test bootstrap. Must run BEFORE any app import: creditflow_common.config,
linkedin.py, and content_client.py read the environment at import time, so
we point them at throwaway/test resources here — SQLite instead of Postgres,
an EPHEMERAL RS256 keypair (the real private key is gitignored, so CI never
has it; tests sign their own tokens with the throwaway key and the app
verifies with its public half), a stub publisher instead of RabbitMQ,
fakeredis for the OAuth CSRF-state store, a fresh Fernet key for
token-at-rest encryption, and fakes for EVERY linkedin.py and
content_client.py function — while both base URLs ALSO point at a dead
address (like the AI service does for OpenRouter) so an unmocked call fails
instantly instead of touching the network. The consumer thread is disabled —
tests call consumer.handle_event directly, exercising the exact function the
broker would. This is why the suite runs in CI with no infra containers and
no secrets.
"""
from __future__ import annotations

import os
import tempfile
import uuid

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_TMP = tempfile.mkdtemp(prefix="creditflow_social_test_")
_DB_FILE = os.path.join(_TMP, f"social_{uuid.uuid4().hex}.db")
_PRIV_PEM = os.path.join(_TMP, "jwt_private.pem")
_PUB_PEM = os.path.join(_TMP, "jwt_public.pem")

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
with open(_PRIV_PEM, "wb") as f:
    f.write(_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
with open(_PUB_PEM, "wb") as f:
    f.write(_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))

os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE}"
os.environ["JWT_PRIVATE_KEY_PATH"] = _PRIV_PEM
os.environ["JWT_PUBLIC_KEY_PATH"] = _PUB_PEM
os.environ["SOCIAL_CONSUMER_ENABLED"] = "0"         # no broker in tests
os.environ["SOCIAL_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ.pop("SOCIAL_CONTENT_TOKEN", None)        # default: fire-payload fallback path
os.environ["REDIS_URL"] = "redis://127.0.0.1:9/0"   # guard: state store is monkeypatched to fakeredis
os.environ["LINKEDIN_CLIENT_ID"] = "test-client-id"
os.environ["LINKEDIN_CLIENT_SECRET"] = "test-client-secret"      # never sent — linkedin.py is mocked
os.environ["LINKEDIN_REDIRECT_URI"] = "http://localhost:5173/linkedin/callback"
os.environ["LINKEDIN_OAUTH_BASE_URL"] = "http://127.0.0.1:9"     # guard: unmocked call fails instantly
os.environ["LINKEDIN_API_BASE_URL"] = "http://127.0.0.1:9"       # guard: unmocked call fails instantly
os.environ["CONTENT_URL"] = "http://127.0.0.1:9"                 # guard: unmocked call fails instantly

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import content_client  # noqa: E402
import database  # noqa: E402
import events  # noqa: E402
import linkedin  # noqa: E402
import main  # noqa: E402
import oauth_state  # noqa: E402


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Fresh in-memory Redis per test for the OAuth CSRF-state store; returns
    the client so tests can inspect/expire state keys."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr(oauth_state, "get_redis", lambda: client)
    return client


@pytest.fixture(autouse=True)
def published_events(monkeypatch):
    """Capture RabbitMQ events instead of talking to a broker; returns
    [(routing_key, payload)]. Routes and the consumer both publish through
    the same events.publish."""
    captured: list[tuple[str, dict]] = []

    def _fake_publish(routing_key: str, payload: dict) -> str:
        captured.append((routing_key, payload))
        return "test-event-id"

    monkeypatch.setattr(events, "publish", _fake_publish)
    return captured


# What the fake LinkedIn hands back by default; tests read these constants
# instead of retyping magic strings.
LI_ACCESS_TOKEN = "li-access-token-secret"
LI_REFRESH_TOKEN = "li-refresh-token-secret"
LI_MEMBER_SUB = "AbC123xyz"
LI_ASSET_URN = "urn:li:digitalmediaAsset:C5522AQGTYER3k3ByHQ"
LI_POST_ID = "urn:li:share:6844785523593134080"


@pytest.fixture(autouse=True)
def linkedin_api(monkeypatch):
    """Replace every linkedin.py function that would do HTTP with a recording
    fake. state["*_calls"] logs arguments; tests inject failures by setting
    state["errors"][fn_name] to an exception instance."""
    state = {
        "exchange_calls": [], "userinfo_calls": [], "register_calls": [],
        "upload_calls": [], "create_calls": [],
        "errors": {},  # fn name -> exception to raise
        "exchange_result": {
            "access_token": LI_ACCESS_TOKEN,
            "expires_in": 5184000,
            "refresh_token": LI_REFRESH_TOKEN,
            "refresh_token_expires_in": 31536000,
            "scope": linkedin.SCOPES,
        },
        "userinfo_result": {"sub": LI_MEMBER_SUB, "name": "Test Member",
                            "email": "member@example.com"},
        "register_result": {"upload_url": "https://uploads.linkedin.test/mediaUpload/1",
                            "asset_urn": LI_ASSET_URN},
        "create_result": {"post_id": LI_POST_ID,
                          "post_url": f"https://www.linkedin.com/feed/update/{LI_POST_ID}/"},
    }

    def _maybe_raise(name):
        if state["errors"].get(name):
            raise state["errors"][name]

    def _exchange(code):
        state["exchange_calls"].append(code)
        _maybe_raise("exchange_code")
        return dict(state["exchange_result"])

    def _userinfo(access_token):
        state["userinfo_calls"].append(access_token)
        _maybe_raise("get_userinfo")
        return dict(state["userinfo_result"])

    def _register(access_token, owner_urn):
        state["register_calls"].append({"access_token": access_token, "owner_urn": owner_urn})
        _maybe_raise("register_image_upload")
        return dict(state["register_result"])

    def _upload(access_token, upload_url, data, content_type=None):
        state["upload_calls"].append({"access_token": access_token, "upload_url": upload_url,
                                      "data": data, "content_type": content_type})
        _maybe_raise("upload_image_binary")

    def _create(access_token, author_urn, text, asset_urn=None):
        state["create_calls"].append({"access_token": access_token, "author_urn": author_urn,
                                      "text": text, "asset_urn": asset_urn})
        _maybe_raise("create_post")
        return dict(state["create_result"])

    monkeypatch.setattr(linkedin, "exchange_code", _exchange)
    monkeypatch.setattr(linkedin, "get_userinfo", _userinfo)
    monkeypatch.setattr(linkedin, "register_image_upload", _register)
    monkeypatch.setattr(linkedin, "upload_image_binary", _upload)
    monkeypatch.setattr(linkedin, "create_post", _create)
    return state


@pytest.fixture(autouse=True)
def content_api(monkeypatch):
    """Replace content_client's HTTP functions with a recording fake backed
    by state["items"] (content_id -> Content-service item dict). Unknown ids
    answer 404 like the real service; state["errors"] injects failures."""
    state = {
        "items": {},
        "get_calls": [], "image_calls": [], "status_calls": [],
        "errors": {},  # fn name -> exception to raise
        "image_result": (b"\x89PNG fake image bytes", "image/png"),
    }

    def _maybe_raise(name):
        if state["errors"].get(name):
            raise state["errors"][name]

    def _get_content(content_id, bearer_token):
        state["get_calls"].append({"content_id": content_id, "bearer_token": bearer_token})
        _maybe_raise("get_content")
        if content_id not in state["items"]:
            raise content_client.ContentClientError(f"content {content_id}: HTTP 404",
                                                    status_code=404)
        return dict(state["items"][content_id])

    def _get_image(image_url, bearer_token=None):
        state["image_calls"].append({"image_url": image_url, "bearer_token": bearer_token})
        _maybe_raise("get_image")
        return state["image_result"]

    def _set_status(content_id, status, bearer_token):
        state["status_calls"].append({"content_id": content_id, "status": status,
                                      "bearer_token": bearer_token})
        _maybe_raise("set_status")
        return {"content_id": content_id, "status": status}

    monkeypatch.setattr(content_client, "get_content", _get_content)
    monkeypatch.setattr(content_client, "get_image", _get_image)
    monkeypatch.setattr(content_client, "set_status", _set_status)
    return state


@pytest.fixture()
def client():
    # Context manager triggers the lifespan (DB init) like a real startup.
    # Each test starts from an empty database — processed_events is
    # service-global, so leftover rows from a previous test would poison
    # assertions.
    with TestClient(main.app) as c:
        from creditflow_common.db import Base
        with database.engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
        yield c


@pytest.fixture()
def db_session(client):
    """Direct DB access for test setup/inspection. Depends on `client` so
    init_db has run."""
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()
