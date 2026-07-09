"""
Test bootstrap. Must run BEFORE any app import: creditflow_common.config reads
the environment at import time, so we point it at throwaway/test resources
here — SQLite instead of Postgres, the repo's real RS256 keypair, fakeredis
instead of Redis, and a stub publisher instead of RabbitMQ. This is why the
suite runs in CI with no infra containers.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import uuid

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DB_FILE = os.path.join(tempfile.gettempdir(), f"creditflow_auth_test_{uuid.uuid4().hex}.db")

os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE}"
os.environ["JWT_PRIVATE_KEY_PATH"] = str(REPO_ROOT / "keys" / "jwt_private.pem")
os.environ["JWT_PUBLIC_KEY_PATH"] = str(REPO_ROOT / "keys" / "jwt_public.pem")
os.environ["AUTH_EXPOSE_DEV_TOKENS"] = "1"  # lets tests grab verification/reset tokens
os.environ["AUTH_LOGIN_MAX_ATTEMPTS"] = "5"
os.environ["AUTH_LOGIN_WINDOW_SECONDS"] = "60"

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import events  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Fresh in-memory Redis per test (isolates sessions + rate-limit counters)."""
    monkeypatch.setattr(store, "_client", fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture(autouse=True)
def published_events(monkeypatch):
    """Capture events instead of talking to RabbitMQ; returns [(routing_key, payload)]."""
    captured: list[tuple[str, dict]] = []

    def _fake_publish(routing_key: str, payload: dict) -> str:
        captured.append((routing_key, payload))
        return "test-event-id"

    monkeypatch.setattr(events, "publish", _fake_publish)
    return captured


@pytest.fixture()
def client():
    # Context manager triggers the lifespan (DB init) like a real startup.
    with TestClient(main.app) as c:
        yield c
