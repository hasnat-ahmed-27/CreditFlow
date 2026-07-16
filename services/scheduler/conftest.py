"""
Test bootstrap. Must run BEFORE any app import: creditflow_common.config and
celery_app read the environment at import time, so we point them at
throwaway/test resources here — SQLite instead of Postgres, an EPHEMERAL
RS256 keypair (the real private key is gitignored, so CI never has it; tests
sign their own tokens with the throwaway key and the app verifies with its
public half), a stub publisher instead of RabbitMQ, and fakeredis for the
fire lock. Celery runs EAGER (task_always_eager + task_eager_propagates via
SCHEDULER_CELERY_EAGER=1): every .delay() executes inline and synchronously,
so no worker, no beat, and no broker connection ever happen — the broker URL
also points at a dead address as a belt-and-braces guard. Beat itself never
runs in tests; the due-scan is exercised by calling tasks.scan_due_schedules
directly (the exact function beat would tick). The consumer thread is
disabled — tests call consumer.handle_event directly, exercising the exact
function the broker would. This is why the suite runs in CI with no infra
containers and no secrets.
"""
from __future__ import annotations

import os
import tempfile
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_TMP = tempfile.mkdtemp(prefix="creditflow_scheduler_test_")
_DB_FILE = os.path.join(_TMP, f"scheduler_{uuid.uuid4().hex}.db")
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
os.environ["SCHEDULER_CONSUMER_ENABLED"] = "0"      # no broker in tests
os.environ["SCHEDULER_CELERY_EAGER"] = "1"          # tasks run inline, no worker/beat
os.environ["CELERY_BROKER_URL"] = "redis://127.0.0.1:9/0"      # guard: never contacted (eager)
os.environ["CELERY_RESULT_BACKEND"] = "redis://127.0.0.1:9/0"  # guard: never contacted (eager)
os.environ["REDIS_URL"] = "redis://127.0.0.1:9/0"   # guard: fire lock is monkeypatched to fakeredis

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
import events  # noqa: E402
import main  # noqa: E402
import tasks  # noqa: E402


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Fresh in-memory Redis per test for the per-occurrence fire lock;
    returns the client so tests can inspect/clear lock keys."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr(tasks, "get_redis", lambda: client)
    return client


@pytest.fixture(autouse=True)
def published_events(monkeypatch):
    """Capture RabbitMQ events instead of talking to a broker; returns
    [(routing_key, payload)]. Both the fire task and (hypothetically) routes
    publish through the same events.publish."""
    captured: list[tuple[str, dict]] = []

    def _fake_publish(routing_key: str, payload: dict) -> str:
        captured.append((routing_key, payload))
        return "test-event-id"

    monkeypatch.setattr(events, "publish", _fake_publish)
    return captured


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
