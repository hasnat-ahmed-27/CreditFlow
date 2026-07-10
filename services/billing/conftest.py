"""
Test bootstrap. Must run BEFORE any app import: creditflow_common.config and
stripe_gateway read the environment at import time, so we point them at
throwaway/test resources here — SQLite instead of Postgres, an EPHEMERAL
RS256 keypair (tests sign their own tokens, the app verifies with the public
half), dummy sk_test_/whsec_ Stripe values, and the poller thread disabled
(tests drive outbox.publish_pending / dunning.apply_due directly).

The Stripe SDK is mocked at the class-method level (Customer.create,
checkout.Session.create, Subscription.retrieve/modify/cancel, Refund.create)
so NO network call can ever happen — but webhook signature verification is
NOT mocked: tests sign payloads with the real Stripe HMAC scheme and the
real stripe.Webhook.construct_event verifies them (pure crypto, no network).
This is why the suite runs in CI with no infra containers and no secrets.
"""
from __future__ import annotations

import itertools
import os
import tempfile
import uuid
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_TMP = tempfile.mkdtemp(prefix="creditflow_billing_test_")
_DB_FILE = os.path.join(_TMP, f"billing_{uuid.uuid4().hex}.db")
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
os.environ["BILLING_POLLER_ENABLED"] = "0"          # no broker/thread in tests
os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"   # test-mode gate passes
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_secret"
os.environ["STRIPE_PRICE_PRO"] = "price_test_pro"
os.environ["STRIPE_PRICE_TEAM"] = "price_test_team"

import pytest  # noqa: E402
import stripe  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
import events  # noqa: E402
import main  # noqa: E402

_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def stripe_stub(monkeypatch):
    """Mock every Stripe SDK call the service makes; record them for
    assertions. Returns dicts shaped like the fields the code reads (the
    real SDK returns dict-subclass StripeObjects, so access is identical)."""
    calls = SimpleNamespace(customers=[], sessions=[], retrieves=[], modifies=[], cancels=[], refunds=[])

    def _customer_create(**kwargs):
        calls.customers.append(kwargs)
        return {"id": f"cus_test{next(_seq)}", "metadata": kwargs.get("metadata", {})}

    def _session_create(**kwargs):
        calls.sessions.append(kwargs)
        return {"id": f"cs_test{next(_seq)}", "url": "https://checkout.stripe.com/c/pay/cs_test"}

    def _sub_retrieve(sub_id, **kwargs):
        calls.retrieves.append(sub_id)
        return {"id": sub_id, "items": {"data": [{"id": "si_test1", "price": {"id": "price_test_pro"}}]}}

    def _sub_modify(sub_id, **kwargs):
        calls.modifies.append({"id": sub_id, **kwargs})
        return {"id": sub_id, "status": "active"}

    def _sub_cancel(sub_id, **kwargs):
        calls.cancels.append({"id": sub_id, **kwargs})
        return {"id": sub_id, "status": "canceled"}

    def _refund_create(**kwargs):
        calls.refunds.append(kwargs)
        return {
            "id": f"re_test{next(_seq)}",
            "payment_intent": kwargs.get("payment_intent"),
            "amount": kwargs.get("amount", 2900),
            "currency": "usd",
            "status": "succeeded",
        }

    monkeypatch.setattr(stripe.Customer, "create", _customer_create)
    monkeypatch.setattr(stripe.checkout.Session, "create", _session_create)
    monkeypatch.setattr(stripe.Subscription, "retrieve", _sub_retrieve)
    monkeypatch.setattr(stripe.Subscription, "modify", _sub_modify)
    monkeypatch.setattr(stripe.Subscription, "cancel", _sub_cancel)
    monkeypatch.setattr(stripe.Refund, "create", _refund_create)
    return calls


@pytest.fixture(autouse=True)
def published_events(monkeypatch):
    """Capture what the outbox poller hands to RabbitMQ instead of talking to
    a broker; returns [(routing_key, payload, event_id)]. Individual tests
    re-patch events.publish to simulate a broker outage (return None)."""
    captured: list[tuple[str, dict, str]] = []

    def _fake_publish(routing_key: str, payload: dict, event_id: str | None = None) -> str:
        captured.append((routing_key, payload, event_id))
        return event_id or "test-event-id"

    monkeypatch.setattr(events, "publish", _fake_publish)
    return captured


@pytest.fixture()
def client():
    # Context manager triggers the lifespan (Stripe test-key gate + DB init)
    # like a real startup. Each test starts from an empty database — the
    # outbox/event tables are service-global, so leftover rows from a previous
    # test would poison count-based assertions.
    with TestClient(main.app) as c:
        from creditflow_common.db import Base
        with database.engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
        yield c


@pytest.fixture()
def db_session(client):
    """Direct DB access for test setup/inspection (e.g. expiring a grace
    period). Depends on `client` so init_db has run."""
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()
