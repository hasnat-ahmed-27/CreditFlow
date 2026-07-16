"""
Test bootstrap. Must run BEFORE any app import: creditflow_common.config and
clients.py read the environment at import time, so we point them at
throwaway/test resources here — SQLite instead of Postgres, an EPHEMERAL
RS256 keypair (the real private key is gitignored, so CI never has it; tests
sign their own admin/tenant-admin/member tokens with the throwaway key to
prove the role gate, and the app verifies with its public half), fakeredis
for the active-session (jti) store, and recording fakes for EVERY clients.py
function — while all three service base URLs ALSO point at a dead address
(like Social does for LinkedIn) so an unmocked call fails instantly instead
of touching the network. There is no publisher to stub: this service
publishes nothing (spec: "Publishes: none"). The consumer threads are
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

_TMP = tempfile.mkdtemp(prefix="creditflow_admin_test_")
_DB_FILE = os.path.join(_TMP, f"admin_{uuid.uuid4().hex}.db")
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
os.environ["ADMIN_CONSUMER_ENABLED"] = "0"          # no broker in tests
os.environ["REDIS_URL"] = "redis://127.0.0.1:9/0"   # guard: session store is monkeypatched to fakeredis
os.environ["USER_URL"] = "http://127.0.0.1:9"       # guard: unmocked call fails instantly
os.environ["CREDITS_URL"] = "http://127.0.0.1:9"    # guard: unmocked call fails instantly
os.environ["USAGE_URL"] = "http://127.0.0.1:9"      # guard: unmocked call fails instantly

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import clients  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
import sessions  # noqa: E402

# Originals captured BEFORE the autouse fixture patches them — the dead-URL
# guard test calls one directly to prove an unmocked downstream call fails
# instantly instead of reaching the network.
REAL_GET_ACCOUNT = clients.get_account
REAL_GET_CREDIT_BALANCE = clients.get_credit_balance


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Fresh in-memory Redis per test for the active-session (jti) store;
    returns the client so tests can seed/inspect session keys exactly the
    way Auth's store.py writes them."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr(sessions, "get_redis", lambda: client)
    return client


@pytest.fixture(autouse=True)
def downstream(monkeypatch):
    """Replace every clients.py function that would do HTTP with a recording
    fake. state["*_calls"] logs arguments; tests inject failures by setting
    state["errors"][fn_name] to an exception instance; happy-path payloads
    live in state["*_result"]."""
    state = {
        "account_calls": [], "members_calls": [], "balance_calls": [], "usage_calls": [],
        "errors": {},  # fn name -> exception to raise
        "account_result": {"account_id": "", "name": "Acme", "plan_tier": "pro",
                           "type": "team", "seat_count": 3},
        "members_result": {"members": [{"user_id": "u1", "role": "owner"},
                                       {"user_id": "u2", "role": "member"}]},
        "balance_result": {"balance": 750},
        "usage_result": {"tokens_used": 12000, "cost_usd": 1.44},
    }

    def _maybe_raise(name):
        if state["errors"].get(name):
            raise state["errors"][name]

    def _account(account_id, bearer_token):
        state["account_calls"].append({"account_id": account_id, "bearer_token": bearer_token})
        _maybe_raise("get_account")
        return {**state["account_result"], "account_id": account_id}

    def _members(account_id, bearer_token):
        state["members_calls"].append({"account_id": account_id, "bearer_token": bearer_token})
        _maybe_raise("get_members")
        return dict(state["members_result"])

    def _balance(bearer_token):
        state["balance_calls"].append({"bearer_token": bearer_token})
        _maybe_raise("get_credit_balance")
        return dict(state["balance_result"])

    def _usage(bearer_token):
        state["usage_calls"].append({"bearer_token": bearer_token})
        _maybe_raise("get_usage_summary")
        return dict(state["usage_result"])

    monkeypatch.setattr(clients, "get_account", _account)
    monkeypatch.setattr(clients, "get_members", _members)
    monkeypatch.setattr(clients, "get_credit_balance", _balance)
    monkeypatch.setattr(clients, "get_usage_summary", _usage)
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
