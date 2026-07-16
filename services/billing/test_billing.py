"""
Billing service tests: webhook signature verification + persist-before-
process, outbox atomicity (state row and event row commit/roll back
together), the poller (publishes, marks, safe to re-run, tolerates a dead
broker), checkout/customer creation, prorated plan changes, dunning
(grace period -> subscription.downgraded), the refund flow emitting
refund.issued, and the Owner-only invoice history endpoint.

No infra: SQLite via conftest, Stripe SDK mocked (signature verification is
REAL — payloads are HMAC-signed the way Stripe signs them), publisher stubbed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from creditflow_common import jwt_utils

import dunning
import outbox
import webhooks
from models import Invoice, OutboxEvent, Refund, Subscription, SubscriptionEvent, utcnow

WEBHOOK_SECRET = "whsec_test_secret"  # matches conftest env


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(account_id: str, role: str = "owner", user_id: str | None = None) -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues."""
    token, _ = jwt_utils.sign_access_token(user_id or _uid(), account_id, role)
    return {"Authorization": f"Bearer {token}"}


def _event(etype: str, obj: dict, event_id: str | None = None) -> dict:
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex}",
        "object": "event",
        "type": etype,
        "data": {"object": obj},
    }


def _post_webhook(client, event: dict, sig_header: str | None = None):
    """POST a webhook signed exactly the way Stripe signs: HMAC-SHA256 over
    '<timestamp>.<payload>' with the endpoint secret. The app verifies it
    with the real stripe.Webhook.construct_event."""
    payload = json.dumps(event).encode("utf-8")
    if sig_header is None:
        t = int(time.time())
        mac = hmac.new(WEBHOOK_SECRET.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
        sig_header = f"t={t},v1={mac}"
    return client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": sig_header})


def _outbox_rows(db, routing_key: str | None = None) -> list[OutboxEvent]:
    q = select(OutboxEvent).order_by(OutboxEvent.created_at, OutboxEvent.id)
    if routing_key:
        q = q.where(OutboxEvent.routing_key == routing_key)
    return db.scalars(q).all()


def _subscription(db, account_id: str) -> Subscription | None:
    db.expire_all()
    return db.scalar(select(Subscription).where(Subscription.account_id == account_id))


def _setup_paid_account(client, db, plan: str = "pro") -> tuple[str, str]:
    """Realistic bootstrap: owner starts checkout (Stripe customer created +
    stored), then Stripe confirms via checkout.session.completed."""
    account_id = _uid()
    r = client.post("/billing/checkout", json={"plan": plan}, headers=_auth(account_id))
    assert r.status_code == 201, r.text
    customer_id = _subscription(db, account_id).stripe_customer_id
    r = _post_webhook(client, _event("checkout.session.completed", {
        "id": f"cs_{uuid.uuid4().hex[:8]}",
        "customer": customer_id,
        "subscription": f"sub_{uuid.uuid4().hex[:8]}",
        "client_reference_id": account_id,
        "metadata": {"account_id": account_id, "plan": plan},
    }))
    assert r.status_code == 200, r.text
    return account_id, customer_id


def _paid_invoice(client, db, customer_id: str, amount: int = 2900) -> dict:
    inv_id = f"in_{uuid.uuid4().hex[:8]}"
    r = _post_webhook(client, _event("invoice.paid", {
        "id": inv_id,
        "customer": customer_id,
        "payment_intent": f"pi_{uuid.uuid4().hex[:8]}",
        "amount_due": amount,
        "amount_paid": amount,
        "currency": "usd",
        "hosted_invoice_url": "https://invoice.stripe.com/i/test",
    }))
    assert r.status_code == 200, r.text
    db.expire_all()
    return db.scalar(select(Invoice).where(Invoice.stripe_invoice_id == inv_id)).__dict__.copy()


# --------------------------------------------------------------------------
# Webhook: signature verification + persist-before-process
# --------------------------------------------------------------------------

def test_webhook_rejects_bad_signature(client, db_session):
    r = _post_webhook(client, _event("invoice.paid", {"id": "in_x"}), sig_header="t=1,v1=deadbeef")
    assert r.status_code == 400
    assert db_session.scalars(select(SubscriptionEvent)).all() == []  # nothing persisted


def test_webhook_persisted_even_when_type_is_ignored(client, db_session):
    """Every verified webhook lands in subscription_events BEFORE processing —
    including types we take no action on (audit trail + §7 write side)."""
    event = _event("customer.created", {"id": "cus_whatever"})
    r = _post_webhook(client, event)
    assert r.status_code == 200

    row = db_session.scalar(select(SubscriptionEvent).where(SubscriptionEvent.stripe_event_id == event["id"]))
    assert row is not None
    assert row.type == "customer.created"
    assert json.loads(row.payload)["id"] == event["id"]  # full raw payload kept
    assert row.processed_at is not None
    assert row.note.startswith("ignored")
    assert _outbox_rows(db_session) == []  # no event staged for an ignored type


def test_webhook_persisted_before_processing_survives_processing_crash(client, db_session):
    """The core persist-before-process guarantee: if processing explodes, the
    raw event is already committed (processed_at NULL) and the state change +
    outbox row roll back TOGETHER. Stripe's redelivery then completes it."""
    account_id, customer_id = _setup_paid_account(client, db_session)
    baseline_outbox = len(_outbox_rows(db_session))
    event = _event("invoice.payment_failed", {
        "id": f"in_{uuid.uuid4().hex[:8]}", "customer": customer_id,
        "amount_due": 2900, "currency": "usd",
    })

    real_process = webhooks.process_event
    with pytest.MonkeyPatch.context() as mp:
        def explode_after_staging(db, evt):
            real_process(db, evt)  # stages the state change AND the outbox row
            raise RuntimeError("boom after staging")
        mp.setattr(webhooks, "process_event", explode_after_staging)
        r = _post_webhook(client, event)
    assert r.status_code == 500

    # Persisted before processing:
    row = db_session.scalar(select(SubscriptionEvent).where(SubscriptionEvent.stripe_event_id == event["id"]))
    assert row is not None and row.processed_at is None
    # ...but the transaction rolled back as one unit: no state change, no outbox row.
    sub = _subscription(db_session, account_id)
    assert sub.status == "active" and sub.grace_expires_at is None
    assert len(_outbox_rows(db_session)) == baseline_outbox

    # Stripe redelivers (same event id) -> processing completes this time.
    r = _post_webhook(client, event)
    assert r.status_code == 200
    db_session.expire_all()
    row = db_session.scalar(select(SubscriptionEvent).where(SubscriptionEvent.stripe_event_id == event["id"]))
    assert row.processed_at is not None
    assert _subscription(db_session, account_id).status == "past_due"
    assert len(_outbox_rows(db_session, "payment.failed")) == 1


def test_duplicate_webhook_delivery_is_a_noop(client, db_session):
    account_id, customer_id = _setup_paid_account(client, db_session)
    event = _event("invoice.paid", {
        "id": f"in_{uuid.uuid4().hex[:8]}", "customer": customer_id,
        "payment_intent": "pi_dup", "amount_due": 2900, "amount_paid": 2900, "currency": "usd",
    })
    assert _post_webhook(client, event).status_code == 200
    r = _post_webhook(client, event)  # Stripe redelivers a processed event
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"
    assert len(_outbox_rows(db_session, "invoice.paid")) == 1  # not processed twice


# --------------------------------------------------------------------------
# Outbox atomicity: state row + event row committed together
# --------------------------------------------------------------------------

def test_invoice_paid_commits_state_and_outbox_row_atomically(client, db_session):
    account_id, customer_id = _setup_paid_account(client, db_session)
    _paid_invoice(client, db_session, customer_id)

    # Same transaction produced both: the invoice row AND the unpublished event.
    invoice = db_session.scalar(select(Invoice).where(Invoice.account_id == account_id))
    assert invoice.status == "paid" and invoice.amount_paid == 2900
    rows = _outbox_rows(db_session, "invoice.paid")
    assert len(rows) == 1
    assert rows[0].published_at is None  # publishing is the POLLER's job, later
    payload = json.loads(rows[0].payload)
    assert payload["account_id"] == account_id
    assert payload["amount_paid"] == 2900


# --------------------------------------------------------------------------
# The poller: publish, mark, re-run safely, survive a dead broker
# --------------------------------------------------------------------------

def test_poller_publishes_unpublished_rows_and_marks_them(client, db_session, published_events):
    account_id, customer_id = _setup_paid_account(client, db_session)
    _paid_invoice(client, db_session, customer_id)
    pending = _outbox_rows(db_session)
    assert all(r.published_at is None for r in pending)

    published = outbox.publish_pending(db_session)
    assert published == len(pending)
    # Everything went to the (stubbed) broker with the outbox row id as the
    # bus event_id — the consumer-side dedup key.
    assert [(rk, eid) for rk, _, eid in published_events] == [(r.routing_key, r.id) for r in pending]
    db_session.expire_all()
    assert all(r.published_at is not None for r in _outbox_rows(db_session))

    # Running the poller again is safe: nothing left, nothing re-published.
    assert outbox.publish_pending(db_session) == 0
    assert len(published_events) == len(pending)


def test_poller_defers_rows_when_broker_is_down(client, db_session, published_events):
    import events as events_module
    account_id, customer_id = _setup_paid_account(client, db_session)
    _paid_invoice(client, db_session, customer_id)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(events_module, "publish", lambda *a, **k: None)  # broker unreachable
        assert outbox.publish_pending(db_session) == 0

    db_session.expire_all()
    rows = _outbox_rows(db_session)
    assert all(r.published_at is None for r in rows)  # nothing lost, nothing marked
    assert rows[0].attempts == 1                      # the attempt was recorded

    # Broker back (conftest stub active again) -> the same rows drain.
    assert outbox.publish_pending(db_session) == len(rows)
    db_session.expire_all()
    assert all(r.published_at is not None for r in _outbox_rows(db_session))


# --------------------------------------------------------------------------
# Checkout + prorated plan change
# --------------------------------------------------------------------------

def test_checkout_creates_and_stores_stripe_customer(client, db_session, stripe_stub):
    account_id = _uid()
    r = client.post("/billing/checkout", json={"plan": "pro"}, headers=_auth(account_id))
    assert r.status_code == 201, r.text
    assert r.json()["checkout_url"].startswith("https://checkout.stripe.com/")

    sub = _subscription(db_session, account_id)
    assert sub.stripe_customer_id is not None and sub.stripe_customer_id.startswith("cus_test")
    assert stripe_stub.customers[0]["metadata"]["account_id"] == account_id
    assert stripe_stub.sessions[0]["metadata"] == {"account_id": account_id, "plan": "pro"}

    # Second checkout reuses the stored customer instead of minting another.
    r = client.post("/billing/checkout", json={"plan": "team"}, headers=_auth(account_id))
    assert r.status_code == 201
    assert len(stripe_stub.customers) == 1

    # Members can't start checkout — money is the owner's call.
    r = client.post("/billing/checkout", json={"plan": "pro"}, headers=_auth(account_id, role="member"))
    assert r.status_code == 403


def test_plan_change_uses_stripe_proration_and_stages_event(client, db_session, stripe_stub):
    account_id, _ = _setup_paid_account(client, db_session, plan="pro")

    r = client.post("/billing/plan", json={"plan": "team"}, headers=_auth(account_id))
    assert r.status_code == 200, r.text
    assert stripe_stub.modifies[0]["proration_behavior"] == "create_prorations"
    assert stripe_stub.modifies[0]["items"][0]["price"] == "price_test_team"
    assert _subscription(db_session, account_id).plan == "team"

    rows = _outbox_rows(db_session, "subscription.updated")
    assert json.loads(rows[-1].payload)["change"] == "owner_plan_change"
    assert json.loads(rows[-1].payload)["prorated"] is True

    # Downgrade to free = prorated cancel; local row survives on the free tier.
    r = client.post("/billing/plan", json={"plan": "free"}, headers=_auth(account_id))
    assert r.status_code == 200
    assert stripe_stub.cancels[0]["prorate"] is True
    sub = _subscription(db_session, account_id)
    assert sub.plan == "free" and sub.stripe_subscription_id is None


# --------------------------------------------------------------------------
# Dunning: grace period on payment_failed -> subscription.downgraded
# --------------------------------------------------------------------------

def test_payment_failed_starts_grace_and_dunning_downgrades_when_unresolved(client, db_session):
    account_id, customer_id = _setup_paid_account(client, db_session)
    r = _post_webhook(client, _event("invoice.payment_failed", {
        "id": f"in_{uuid.uuid4().hex[:8]}", "customer": customer_id,
        "amount_due": 2900, "currency": "usd",
    }))
    assert r.status_code == 200

    sub = _subscription(db_session, account_id)
    assert sub.status == "past_due" and sub.grace_expires_at is not None
    assert len(_outbox_rows(db_session, "payment.failed")) == 1

    # Grace not yet expired -> dunning leaves it alone.
    assert dunning.apply_due(db_session) == []

    # Fast-forward: grace expired, still unresolved -> downgrade to free.
    sub = _subscription(db_session, account_id)
    sub.grace_expires_at = utcnow() - timedelta(hours=1)
    db_session.commit()
    assert dunning.apply_due(db_session) == [account_id]

    sub = _subscription(db_session, account_id)
    assert sub.plan == "free" and sub.status == "active" and sub.grace_expires_at is None
    rows = _outbox_rows(db_session, "subscription.downgraded")
    assert len(rows) == 1
    assert json.loads(rows[0].payload)["reason"] == "dunning_grace_expired"

    # Idempotent: the account left past_due, a second sweep finds nothing.
    assert dunning.apply_due(db_session) == []


def test_invoice_paid_during_grace_resolves_dunning(client, db_session):
    account_id, customer_id = _setup_paid_account(client, db_session)
    _post_webhook(client, _event("invoice.payment_failed", {
        "id": "in_fail1", "customer": customer_id, "amount_due": 2900, "currency": "usd",
    }))
    assert _subscription(db_session, account_id).status == "past_due"

    _paid_invoice(client, db_session, customer_id)
    sub = _subscription(db_session, account_id)
    assert sub.status == "active" and sub.grace_expires_at is None
    payload = json.loads(_outbox_rows(db_session, "invoice.paid")[-1].payload)
    assert payload["dunning_recovered"] is True


# --------------------------------------------------------------------------
# Refunds -> refund.issued
# --------------------------------------------------------------------------

def test_refund_flow_records_and_emits_refund_issued(client, db_session, stripe_stub, published_events):
    account_id, customer_id = _setup_paid_account(client, db_session)
    _paid_invoice(client, db_session, customer_id)
    inv = client.get("/billing/invoices", headers=_auth(account_id)).json()["invoices"][0]

    r = client.post("/billing/refunds", json={"invoice_id": inv["invoice_id"], "reason": "requested_by_customer"},
                    headers=_auth(account_id))
    assert r.status_code == 201, r.text
    assert stripe_stub.refunds[0]["payment_intent"] is not None

    refund = db_session.scalar(select(Refund).where(Refund.account_id == account_id))
    assert refund is not None and refund.status == "succeeded" and refund.amount == 2900

    # Refund row + refund.issued staged in the same commit; poller delivers it.
    rows = _outbox_rows(db_session, "refund.issued")
    assert len(rows) == 1 and rows[0].published_at is None
    outbox.publish_pending(db_session)
    issued = [p for rk, p, _ in published_events if rk == "refund.issued"]
    assert issued and issued[0]["account_id"] == account_id and issued[0]["refund_id"] == refund.id

    # Refunds are owner-only, and other accounts' invoices are invisible.
    r = client.post("/billing/refunds", json={"invoice_id": inv["invoice_id"]},
                    headers=_auth(account_id, role="admin"))
    assert r.status_code == 403
    r = client.post("/billing/refunds", json={"invoice_id": inv["invoice_id"]}, headers=_auth(_uid()))
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Owner-only invoice history
# --------------------------------------------------------------------------

def test_invoice_history_is_owner_only(client, db_session):
    account_id, customer_id = _setup_paid_account(client, db_session)
    _paid_invoice(client, db_session, customer_id)

    r = client.get("/billing/invoices", headers=_auth(account_id, role="owner"))
    assert r.status_code == 200
    invoices = r.json()["invoices"]
    assert len(invoices) == 1 and invoices[0]["status"] == "paid"

    assert client.get("/billing/invoices", headers=_auth(account_id, role="admin")).status_code == 403
    assert client.get("/billing/invoices", headers=_auth(account_id, role="member")).status_code == 403
    assert client.get("/billing/invoices").status_code == 401

    # Another account's owner sees their own (empty) history, not this one's.
    r = client.get("/billing/invoices", headers=_auth(_uid(), role="owner"))
    assert r.status_code == 200 and r.json()["invoices"] == []
