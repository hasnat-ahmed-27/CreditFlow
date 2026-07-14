"""
Test bootstrap. Must run BEFORE any app import: creditflow_common.config reads
the environment at import time, so we point it at throwaway/test resources
here — SQLite instead of Postgres, an EPHEMERAL RS256 keypair (the real
private key is gitignored, so CI never has it; tests sign their own tokens
with the throwaway key and the app verifies with its public half), fakeredis
(async — this service's pub/sub fan-out) instead of Redis, a stub publisher
instead of RabbitMQ, a stub quota check instead of the Usage service, and a
fake OpenRouter stream instead of the real API (OPENROUTER_BASE_URL also
points at a dead address as a belt-and-braces guard: no test can ever hit
the network). AI_EAGER_WORKER=1 makes POST /generations run the worker
inline, so by the time a test opens the SSE stream every chunk is already in
the Redis replay buffer — deterministic order, no sleeps. This is why the
suite runs in CI with no infra containers and no secrets.
"""
from __future__ import annotations

import os
import tempfile
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_TMP = tempfile.mkdtemp(prefix="creditflow_ai_test_")
_DB_FILE = os.path.join(_TMP, f"ai_{uuid.uuid4().hex}.db")
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
os.environ["AI_EAGER_WORKER"] = "1"            # worker runs inline in POST (see docstring)
os.environ["OPENROUTER_API_KEY"] = "test-key"  # never sent anywhere — stream_chat is mocked
os.environ["OPENROUTER_BASE_URL"] = "http://127.0.0.1:9"  # guard: unmocked call fails instantly
os.environ["USAGE_URL"] = "http://127.0.0.1:9"            # guard: unmocked quota check fails instantly

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
import events  # noqa: E402
import main  # noqa: E402
import openrouter  # noqa: E402
import store  # noqa: E402
import usage_client  # noqa: E402


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Fresh in-memory Redis per test, one FakeRedis per event loop (async
    clients bind to the loop they first run on; tests that drive the worker
    via asyncio.run use a different loop than the TestClient), all sharing
    one FakeServer so the data is common. Returns the server so tests can
    open their own inspection handle against it."""
    import asyncio

    server = fakeredis.FakeServer()
    clients: dict[int, fakeredis.aioredis.FakeRedis] = {}

    def _get_redis():
        loop_id = id(asyncio.get_running_loop())
        if loop_id not in clients:
            clients[loop_id] = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
        return clients[loop_id]

    monkeypatch.setattr(store, "get_redis", _get_redis)
    return server


@pytest.fixture(autouse=True)
def published_events(monkeypatch):
    """Capture RabbitMQ events instead of talking to a broker; returns
    [(routing_key, payload)]."""
    captured: list[tuple[str, dict]] = []

    def _fake_publish(routing_key: str, payload: dict) -> str:
        captured.append((routing_key, payload))
        return "test-event-id"

    monkeypatch.setattr(events, "publish", _fake_publish)
    return captured


@pytest.fixture(autouse=True)
def quota(monkeypatch):
    """Stub the synchronous Usage quota check. Tests flip state["allowed"]
    (deny path) or set state["error"] (Usage down); state["calls"] records
    the estimated_tokens of every check made."""
    state = {"allowed": True, "error": None, "calls": []}

    async def _fake_check(bearer_token: str, estimated_tokens: int) -> dict:
        state["calls"].append(estimated_tokens)
        if state["error"] is not None:
            raise state["error"]
        return {
            "allowed": state["allowed"],
            "used_tokens": 0 if state["allowed"] else 1000,
            "quota_tokens": 1000,
            "remaining_tokens": 1000 if state["allowed"] else 0,
        }

    monkeypatch.setattr(usage_client, "check_quota", _fake_check)
    return state


DEFAULT_TOKENS = ["Hello", " ", "world"]
DEFAULT_USAGE = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10, "cost_usd": 0.00012}


@pytest.fixture(autouse=True)
def openrouter_stream(monkeypatch):
    """Replace openrouter.stream_chat with a fake async generator. Default:
    three token chunks + provider usage. Tests swap state["factory"] for
    error/cancellation scenarios; state["calls"] records every invocation."""
    async def _default(model: str, prompt: str, max_tokens=None):
        for tok in DEFAULT_TOKENS:
            yield ("token", tok)
        yield ("usage", dict(DEFAULT_USAGE))

    state = {"factory": _default, "calls": []}

    def _fake_stream(model: str, prompt: str, max_tokens=None):
        state["calls"].append({"model": model, "prompt": prompt, "max_tokens": max_tokens})
        return state["factory"](model, prompt, max_tokens)

    monkeypatch.setattr(openrouter, "stream_chat", _fake_stream)
    return state


@pytest.fixture()
def client():
    # Context manager triggers the lifespan (DB init) like a real startup.
    # Each test starts from an empty database.
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
