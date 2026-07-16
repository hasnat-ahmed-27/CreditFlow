"""
Stripe webhook endpoint + event processing.

Flow (deliberately TWO transactions — spec: "persist every Stripe webhook
event ... BEFORE further processing"):

  Txn A  verify signature, INSERT the raw event into subscription_events,
         COMMIT. From this moment the event cannot be lost, whatever happens
         next. The unique stripe_event_id makes duplicate deliveries no-ops.
  Txn B  apply the domain state change AND outbox.stage() the events to
         publish AND set processed_at — all in ONE transaction. If anything
         throws, the whole txn rolls back: no half-applied state, no orphan
         outbox row, processed_at stays NULL. We return 500, Stripe
         redelivers, and the retry runs Txn B again against the already
         persisted event.

Today Stripe calls this endpoint directly; later the API Gateway will verify
+ dedup + relay (spec Service 1) — the persist-before-process contract here
stays the same either way.

Consumes (RabbitMQ): none — per spec, billing receives via webhooks only.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from creditflow_common import config

import database
import outbox
import stripe_gateway
from models import Invoice, Subscription, SubscriptionEvent, utcnow

logger = logging.getLogger("billing.webhooks")

router = APIRouter(tags=["webhooks"])

# Dunning grace period, started on the first invoice.payment_failed.
GRACE_PERIOD_SECONDS = int(config.env("BILLING_GRACE_PERIOD_SECONDS", str(7 * 24 * 3600)))

# Stripe status -> our lifecycle. Anything unpaid-ish maps to past_due so
# dunning owns it; terminal states map to canceled.
_STATUS_MAP = {
    "active": "active",
    "trialing": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "canceled": "canceled",
    "incomplete_expired": "canceled",
}


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(database.get_db)) -> dict:
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        stripe_gateway.construct_webhook_event(payload, sig_header)  # verify only
    except Exception:  # noqa: BLE001 — bad signature / malformed payload
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature")
    # Process the raw JSON as plain dicts (the SDK's StripeObject wrapper is
    # awkward to traverse and adds nothing once the signature is verified).
    event = json.loads(payload)

    # ---- Txn A: persist the raw event before doing ANYTHING with it -------
    stored = db.scalar(select(SubscriptionEvent).where(SubscriptionEvent.stripe_event_id == event["id"]))
    if stored is None:
        stored = SubscriptionEvent(
            stripe_event_id=event["id"],
            type=event["type"],
            payload=payload.decode("utf-8"),
        )
        db.add(stored)
        try:
            db.commit()
        except IntegrityError:  # concurrent delivery of the same event won the race
            db.rollback()
            stored = db.scalar(select(SubscriptionEvent).where(SubscriptionEvent.stripe_event_id == event["id"]))
    if stored.processed_at is not None:
        # Fully handled before — duplicate delivery is acknowledged, not re-run.
        return {"status": "duplicate", "stripe_event_id": event["id"]}

    # ---- Txn B: state change + outbox rows + processed marker, atomically --
    try:
        note = process_event(db, event)
        stored.processed_at = utcnow()
        stored.note = note
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()  # state + outbox + marker all undone together
        logger.exception("processing failed for %s (%s); stored for retry", event["id"], event["type"])
        # Non-2xx makes Stripe redeliver; the retry finds the persisted row
        # with processed_at NULL and re-runs processing only.
        raise HTTPException(status_code=500, detail="Webhook processing failed; event stored, will be retried")

    return {"status": "processed", "stripe_event_id": event["id"], "note": note}


def process_event(db: Session, event: dict) -> str:
    """Apply one verified, persisted Stripe event. Stages DB changes and
    outbox rows on the caller's session — the caller commits (or rolls back)
    everything as one transaction. Returns a short note for the event row."""
    etype = event["type"]
    obj = event["data"]["object"]
    if etype == "checkout.session.completed":
        return _on_checkout_completed(db, obj)
    if etype in ("invoice.paid", "invoice.payment_succeeded"):
        return _on_invoice_paid(db, obj)
    if etype == "invoice.payment_failed":
        return _on_invoice_payment_failed(db, obj)
    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        return _on_subscription_changed(db, obj, deleted=etype.endswith("deleted"))
    return f"ignored ({etype})"  # persisted for audit, no action for this type


# --------------------------------------------------------------------------
# Handlers — each stages state + outbox on the open session, never commits.
# --------------------------------------------------------------------------

def _subscription_by_customer(db: Session, customer_id: str | None) -> Subscription | None:
    if not customer_id:
        return None
    return db.scalar(select(Subscription).where(Subscription.stripe_customer_id == customer_id))


def _upsert_invoice(db: Session, obj: dict, account_id: str, status: str) -> Invoice:
    """Webhook redeliveries and paid-after-failed both hit the same Stripe
    invoice id — update in place instead of duplicating history."""
    invoice = db.scalar(select(Invoice).where(Invoice.stripe_invoice_id == obj["id"]))
    if invoice is None:
        invoice = Invoice(stripe_invoice_id=obj["id"], account_id=account_id)
        db.add(invoice)
    invoice.stripe_customer_id = obj.get("customer")
    invoice.stripe_payment_intent_id = obj.get("payment_intent")
    invoice.amount_due = obj.get("amount_due") or 0
    invoice.amount_paid = obj.get("amount_paid") or 0
    invoice.currency = obj.get("currency") or "usd"
    invoice.status = status
    invoice.hosted_invoice_url = obj.get("hosted_invoice_url")
    return invoice


def _on_checkout_completed(db: Session, obj: dict) -> str:
    """First purchase: link the Stripe customer/subscription to the account
    and activate the chosen plan."""
    metadata = obj.get("metadata") or {}
    account_id = obj.get("client_reference_id") or metadata.get("account_id")
    if not account_id:
        return "no account reference on checkout session"
    sub = db.scalar(select(Subscription).where(Subscription.account_id == account_id))
    if sub is None:
        sub = Subscription(account_id=account_id)
        db.add(sub)
    sub.stripe_customer_id = obj.get("customer") or sub.stripe_customer_id
    sub.stripe_subscription_id = obj.get("subscription")
    sub.plan = metadata.get("plan") or sub.plan
    sub.status = "active"
    sub.grace_expires_at = None
    outbox.stage(db, "subscription.updated", {
        "account_id": account_id,
        "plan": sub.plan,
        "status": sub.status,
        "change": "checkout_completed",
    })
    return f"activated plan {sub.plan}"


def _on_invoice_paid(db: Session, obj: dict) -> str:
    sub = _subscription_by_customer(db, obj.get("customer"))
    if sub is None:
        return f"no account for customer {obj.get('customer')}"
    invoice = _upsert_invoice(db, obj, sub.account_id, status="paid")
    # A successful payment resolves dunning: back to active, grace cleared.
    recovered = sub.status == "past_due"
    sub.status = "active"
    sub.grace_expires_at = None
    outbox.stage(db, "invoice.paid", {
        "account_id": sub.account_id,
        "plan": sub.plan,
        "stripe_invoice_id": invoice.stripe_invoice_id,
        "amount_paid": invoice.amount_paid,
        "currency": invoice.currency,
        "dunning_recovered": recovered,
    })
    return "invoice paid" + (" (dunning recovered)" if recovered else "")


def _on_invoice_payment_failed(db: Session, obj: dict) -> str:
    """Dunning entry point: record the failed invoice, put the subscription
    in past_due, and start the grace period — kept from the FIRST failure
    (Stripe retries produce more failed events; they must not extend it)."""
    sub = _subscription_by_customer(db, obj.get("customer"))
    if sub is None:
        return f"no account for customer {obj.get('customer')}"
    invoice = _upsert_invoice(db, obj, sub.account_id, status="payment_failed")
    sub.status = "past_due"
    if sub.grace_expires_at is None:
        sub.grace_expires_at = utcnow() + timedelta(seconds=GRACE_PERIOD_SECONDS)
    outbox.stage(db, "payment.failed", {
        "account_id": sub.account_id,
        "plan": sub.plan,
        "stripe_invoice_id": invoice.stripe_invoice_id,
        "amount_due": invoice.amount_due,
        "currency": invoice.currency,
        "grace_expires_at": sub.grace_expires_at.isoformat(),
    })
    return "payment failed; grace period running"


def _on_subscription_changed(db: Session, obj: dict, deleted: bool) -> str:
    sub = _subscription_by_customer(db, obj.get("customer"))
    if sub is None:
        return f"no account for customer {obj.get('customer')}"
    if deleted:
        sub.plan = "free"
        sub.status = "canceled"
        sub.stripe_subscription_id = None
        sub.grace_expires_at = None
        change = "subscription_deleted"
    else:
        metadata = obj.get("metadata") or {}
        items = ((obj.get("items") or {}).get("data")) or []
        price_id = (items[0].get("price") or {}).get("id") if items else None
        sub.plan = metadata.get("plan") or stripe_gateway.PLAN_BY_PRICE.get(price_id, sub.plan)
        sub.status = _STATUS_MAP.get(obj.get("status"), sub.status)
        change = "subscription_updated"
    outbox.stage(db, "subscription.updated", {
        "account_id": sub.account_id,
        "plan": sub.plan,
        "status": sub.status,
        "change": change,
    })
    return change
