"""
Test bootstrap. Must run BEFORE any app import: creditflow_common.config and
user_client.py read the environment at import time, so we point them at
throwaway/test resources here — SQLite instead of Postgres, an EPHEMERAL
RS256 keypair (the real private key is gitignored, so CI never has it),
fakeredis instead of Redis, a stub publisher instead of RabbitMQ, and an
in-memory fake of the User service (`accounts` fixture) while USER_URL ALSO
points at a dead address so an unmocked call fails instantly instead of
touching the network. This is why the suite runs in CI with no infra
containers and no secrets.
"""
from __future__ import annotations

import os
import tempfile
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_TMP = tempfile.mkdtemp(prefix="creditflow_auth_test_")
_DB_FILE = os.path.join(_TMP, f"auth_{uuid.uuid4().hex}.db")
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
os.environ["AUTH_EXPOSE_DEV_TOKENS"] = "1"  # lets tests grab verification/reset tokens
os.environ["AUTH_LOGIN_MAX_ATTEMPTS"] = "5"
os.environ["AUTH_LOGIN_WINDOW_SECONDS"] = "60"
os.environ["USER_URL"] = "http://127.0.0.1:9"   # guard: unmocked call fails instantly
os.environ.setdefault("SUPERADMIN_EMAILS", "")  # tests opt in per-case via monkeypatch

import uuid as _uuid  # noqa: E402

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
import events  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402
import user_client  # noqa: E402

# Captured BEFORE the autouse fixture patches them — the dead-URL guard test
# calls one directly to prove an unmocked downstream call fails instantly
# instead of reaching the network.
REAL_ENSURE_INDIVIDUAL_ACCOUNT = user_client.ensure_individual_account
REAL_GET_MEMBERSHIP = user_client.get_membership


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


@pytest.fixture(autouse=True)
def accounts(monkeypatch):
    """In-memory stand-in for the User service's /internal API — the seam
    where Auth learns account_id and role (user_client.py).

    Behaviour mirrors the real endpoints: `ensure` is idempotent per user and
    returns their individual account as owner; `get_membership` answers None
    (the service's 404) for anyone not in `memberships`. Tests seed extra
    accounts with `add_membership(...)` to exercise the account switcher, flip
    `errors[...]` to an exception to exercise the service-down paths, and read
    `calls` to assert what was asked.
    """
    state = {
        "individual": {},    # user_id -> account_id
        "memberships": {},   # (user_id, account_id) -> role
        "errors": {},        # fn name -> exception to raise
        "calls": [],         # (fn name, *args)
    }

    def add_membership(user_id: str, role: str, account_id: str | None = None) -> str:
        account_id = account_id or str(_uuid.uuid4())
        state["memberships"][(user_id, account_id)] = role
        return account_id

    state["add_membership"] = add_membership

    def _maybe_raise(name: str) -> None:
        if state["errors"].get(name):
            raise state["errors"][name]

    def _ensure(user_id: str, email: str) -> dict:
        state["calls"].append(("ensure_individual_account", user_id, email))
        _maybe_raise("ensure_individual_account")
        created = user_id not in state["individual"]
        if created:
            state["individual"][user_id] = add_membership(user_id, "owner")
        account_id = state["individual"][user_id]
        return {"user_id": user_id, "account_id": account_id, "role": "owner",
                "type": "individual", "name": email or user_id,
                "plan_tier": "free", "created": created}

    def _membership(user_id: str, account_id: str) -> dict | None:
        state["calls"].append(("get_membership", user_id, account_id))
        _maybe_raise("get_membership")
        role = state["memberships"].get((user_id, account_id))
        if role is None:
            return None
        return {"user_id": user_id, "account_id": account_id, "role": role,
                "type": "team", "name": "Acme", "plan_tier": "free"}

    monkeypatch.setattr(user_client, "ensure_individual_account", _ensure)
    monkeypatch.setattr(user_client, "get_membership", _membership)
    return state


@pytest.fixture()
def client():
    # Context manager triggers the lifespan (DB init + superadmin reconcile)
    # like a real startup. Each test starts from an empty database so the
    # SUPERADMIN_EMAILS reconcile can't see a previous test's users.
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
