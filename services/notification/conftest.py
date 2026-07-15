"""
Test bootstrap. Must run BEFORE any app import: creditflow_common.config and
mailer.py read parts of the environment at import time, so we point them at
throwaway/test resources here — SQLite instead of Postgres, an EPHEMERAL
RS256 keypair (the real private key is gitignored, so CI never has it; tests
sign their own tokens with the throwaway key and the app verifies with its
public half), a stub publisher instead of RabbitMQ, and recording fakes for
BOTH mailer.py provider functions (send_via_resend / send_via_mailgun) —
while both provider base URLs ALSO point at a dead address (like Social does
for LinkedIn) so an unmocked call fails instantly instead of touching
Resend/Mailgun. mailer.send() itself stays REAL, so the primary->fallback
logic is exercised by the tests. The consumer threads are disabled — tests
call consumer.handle_event directly, exercising the exact function the
broker would. This is why the suite runs in CI with no infra containers and
no secrets. (No Redis in this service, so no fakeredis.)
"""
from __future__ import annotations

import os
import tempfile
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_TMP = tempfile.mkdtemp(prefix="creditflow_notification_test_")
_DB_FILE = os.path.join(_TMP, f"notification_{uuid.uuid4().hex}.db")
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
os.environ["NOTIFICATION_CONSUMER_ENABLED"] = "0"   # no broker in tests
os.environ["RESEND_API_KEY"] = "test-resend-key"          # never sent — mailer fns are mocked
os.environ["MAILGUN_API_KEY"] = "test-mailgun-key"        # never sent — mailer fns are mocked
os.environ["MAILGUN_DOMAIN"] = "mail.creditflow.test"
os.environ["NOTIFY_FROM_EMAIL"] = "CreditFlow <no-reply@creditflow.test>"
os.environ["NOTIFY_APP_BASE_URL"] = "http://localhost:5173"
os.environ["RESEND_API_BASE_URL"] = "http://127.0.0.1:9"   # guard: unmocked call fails instantly
os.environ["MAILGUN_API_BASE_URL"] = "http://127.0.0.1:9"  # guard: unmocked call fails instantly

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
import events  # noqa: E402
import mailer  # noqa: E402
import main  # noqa: E402

# Originals captured BEFORE the autouse fixture patches them — the dead-URL
# guard test calls these directly to prove an unmocked provider call fails
# instantly instead of reaching the network.
REAL_SEND_VIA_RESEND = mailer.send_via_resend
REAL_SEND_VIA_MAILGUN = mailer.send_via_mailgun


@pytest.fixture(autouse=True)
def published_events(monkeypatch):
    """Capture RabbitMQ events instead of talking to a broker; returns
    [(routing_key, payload)]. The consumer publishes notification.sent
    through events.publish."""
    captured: list[tuple[str, dict]] = []

    def _fake_publish(routing_key: str, payload: dict) -> str:
        captured.append((routing_key, payload))
        return "test-event-id"

    monkeypatch.setattr(events, "publish", _fake_publish)
    return captured


# What the fake providers hand back by default; tests read these constants
# instead of retyping magic strings.
RESEND_MESSAGE_ID = "re_test_4923e0a1"
MAILGUN_MESSAGE_ID = "<20260716.12345@mail.creditflow.test>"


@pytest.fixture(autouse=True)
def mail_state(monkeypatch):
    """Replace both mailer.py provider functions with recording fakes.
    state["resend_calls"] / state["mailgun_calls"] log arguments; tests
    inject failures by setting state["errors"]["resend"|"mailgun"] to an
    exception instance. mailer.send() (the primary->fallback orchestration)
    is NOT mocked — the failure-path tests exercise it for real."""
    state = {
        "resend_calls": [], "mailgun_calls": [],
        "errors": {},  # provider name -> exception to raise
        "resend_id": RESEND_MESSAGE_ID,
        "mailgun_id": MAILGUN_MESSAGE_ID,
    }

    def _resend(to, subject, text, html=None):
        state["resend_calls"].append({"to": to, "subject": subject,
                                      "text": text, "html": html})
        if state["errors"].get("resend"):
            raise state["errors"]["resend"]
        return state["resend_id"]

    def _mailgun(to, subject, text, html=None):
        state["mailgun_calls"].append({"to": to, "subject": subject,
                                       "text": text, "html": html})
        if state["errors"].get("mailgun"):
            raise state["errors"]["mailgun"]
        return state["mailgun_id"]

    monkeypatch.setattr(mailer, "send_via_resend", _resend)
    monkeypatch.setattr(mailer, "send_via_mailgun", _mailgun)
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
