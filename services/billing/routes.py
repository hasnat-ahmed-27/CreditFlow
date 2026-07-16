"""
Billing endpoints.

Authorization model: identity comes from the RS256 access token, verified
with the shared public key via creditflow_common.jwt_utils. Unlike the User
service, billing has no membership table — the ROLE is taken from the token
claim (minted by Auth from the User service's account_members, and enforced
again at the Gateway). Money-moving endpoints and invoice history are
OWNER-only (spec requires it for invoice history; we hold plan changes and
refunds to the same bar — an admin must not be able to spend the owner's
money).

Write-path rule (spec §7): any endpoint that changes billing state commits
the state change and its outbox.stage()d event in ONE transaction. Checkout
is the exception that proves the rule — it changes nothing locally (Stripe
confirms via webhook later), so it stages nothing.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from creditflow_common import jwt_utils

import database
import outbox
import schemas
import stripe_gateway
from models import Invoice, Refund, Subscription

router = APIRouter(tags=["billing"])


def current_claims(authorization: str = Header(default="")) -> dict:
    """Verify the Bearer access token's RS256 signature + expiry."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        claims = jwt_utils.verify_token(token)
    except jwt_utils.TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if claims.get("type") != "access":
        raise HTTPException(status_code=401, detail="Not an access token")
    return claims


def require_owner(claims: dict = Depends(current_claims)) -> dict:
    if claims.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Requires the account owner role")
    return claims


def _get_or_create_subscription(db: Session, account_id: str) -> Subscription:
    sub = db.scalar(select(Subscription).where(Subscription.account_id == account_id))
    if sub is None:
        sub = Subscription(account_id=account_id)  # free tier by default
        db.add(sub)
        db.commit()
    return sub


def _subscription_view(sub: Subscription) -> dict:
    return {
        "account_id": sub.account_id,
        "plan": sub.plan,
        "status": sub.status,
        "stripe_customer_id": sub.stripe_customer_id,
        "grace_expires_at": sub.grace_expires_at.isoformat() if sub.grace_expires_at else None,
    }


# --------------------------------------------------------------------------
# Subscription: current state, checkout, plan change
# --------------------------------------------------------------------------

@router.get("/billing/subscription")
def get_subscription(claims: dict = Depends(current_claims), db: Session = Depends(database.get_db)) -> dict:
    """Current plan/status for the caller's account — any member may look
    (the dashboard header shows the plan tier)."""
    return _subscription_view(_get_or_create_subscription(db, claims["account_id"]))


@router.post("/billing/checkout", status_code=201)
def create_checkout(
    body: schemas.CheckoutRequest,
    claims: dict = Depends(require_owner),
    db: Session = Depends(database.get_db),
) -> dict:
    """Start a Stripe Checkout Session for a paid plan. Creates (and stores)
    the Stripe Customer on first use. No local state changes and no events
    here: nothing has been sold until Stripe says so via
    checkout.session.completed / invoice.paid webhooks."""
    account_id = claims["account_id"]
    sub = _get_or_create_subscription(db, account_id)

    if sub.stripe_customer_id is None:
        customer = stripe_gateway.create_customer(account_id)
        sub.stripe_customer_id = customer["id"]
        db.commit()  # store immediately — webhooks join on this id

    session = stripe_gateway.create_checkout_session(sub.stripe_customer_id, body.plan, account_id)
    return {
        "checkout_url": session["url"],
        "checkout_session_id": session["id"],
        "plan": body.plan,
    }


@router.post("/billing/plan")
def change_plan(
    body: schemas.ChangePlanRequest,
    claims: dict = Depends(require_owner),
    db: Session = Depends(database.get_db),
) -> dict:
    """Upgrade/downgrade an EXISTING Stripe subscription with proration;
    plan='free' cancels it (final prorated invoice). Stripe call first, then
    local state + subscription.updated outbox row in one transaction."""
    account_id = claims["account_id"]
    sub = db.scalar(select(Subscription).where(Subscription.account_id == account_id))
    if sub is None or sub.stripe_subscription_id is None:
        raise HTTPException(status_code=409, detail="No active paid subscription — use /billing/checkout first")
    if body.plan == sub.plan:
        raise HTTPException(status_code=409, detail=f"Account is already on the {sub.plan} plan")

    previous_plan = sub.plan
    if body.plan == "free":
        stripe_gateway.cancel_subscription(sub.stripe_subscription_id)
        sub.stripe_subscription_id = None
        sub.grace_expires_at = None
    else:
        stripe_gateway.change_plan(sub.stripe_subscription_id, body.plan)
    sub.plan = body.plan
    sub.status = "active"
    outbox.stage(db, "subscription.updated", {
        "account_id": account_id,
        "plan": sub.plan,
        "status": sub.status,
        "previous_plan": previous_plan,
        "change": "owner_plan_change",
        "prorated": True,
    })
    db.commit()  # state change + event row: atomic
    return _subscription_view(sub)


# --------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------

@router.post("/billing/refunds", status_code=201)
def create_refund(
    body: schemas.RefundRequest,
    claims: dict = Depends(require_owner),
    db: Session = Depends(database.get_db),
) -> dict:
    """Refund a payment via Stripe, then record the refund and stage
    refund.issued in one transaction — the Credits service consumes it to
    claw back any credit grant tied to the original payment."""
    account_id = claims["account_id"]

    invoice = None
    payment_intent_id = body.stripe_payment_intent_id
    if body.invoice_id is not None:
        invoice = db.get(Invoice, body.invoice_id)
        if invoice is None or invoice.account_id != account_id:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.stripe_payment_intent_id is None:
            raise HTTPException(status_code=409, detail="Invoice has no payment to refund")
        payment_intent_id = invoice.stripe_payment_intent_id

    try:
        stripe_refund = stripe_gateway.create_refund(payment_intent_id, body.amount, body.reason)
    except Exception:  # noqa: BLE001 — surface Stripe rejections as a clean 502
        raise HTTPException(status_code=502, detail="Stripe refused the refund")

    refund = Refund(
        account_id=account_id,
        stripe_refund_id=stripe_refund["id"],
        stripe_payment_intent_id=payment_intent_id,
        invoice_id=invoice.id if invoice else None,
        amount=stripe_refund["amount"],
        currency=stripe_refund.get("currency") or "usd",
        status=stripe_refund.get("status") or "pending",
        reason=body.reason,
        requested_by_user_id=claims["sub"],
    )
    db.add(refund)
    db.flush()  # assign refund.id before it goes into the event payload
    outbox.stage(db, "refund.issued", {
        "account_id": account_id,
        "refund_id": refund.id,
        "stripe_refund_id": refund.stripe_refund_id,
        "stripe_payment_intent_id": payment_intent_id,
        "invoice_id": refund.invoice_id,
        "amount": refund.amount,
        "currency": refund.currency,
        "reason": body.reason,
    })
    db.commit()  # refund row + event row: atomic
    return {
        "refund_id": refund.id,
        "stripe_refund_id": refund.stripe_refund_id,
        "amount": refund.amount,
        "currency": refund.currency,
        "status": refund.status,
    }


# --------------------------------------------------------------------------
# Invoice history (Owner-only, per spec)
# --------------------------------------------------------------------------

@router.get("/billing/invoices")
def invoice_history(claims: dict = Depends(require_owner), db: Session = Depends(database.get_db)) -> dict:
    rows = db.scalars(
        select(Invoice).where(Invoice.account_id == claims["account_id"]).order_by(Invoice.created_at.desc())
    ).all()
    return {
        "account_id": claims["account_id"],
        "invoices": [
            {
                "invoice_id": inv.id,
                "stripe_invoice_id": inv.stripe_invoice_id,
                "status": inv.status,
                "amount_due": inv.amount_due,
                "amount_paid": inv.amount_paid,
                "currency": inv.currency,
                "hosted_invoice_url": inv.hosted_invoice_url,
                "created_at": inv.created_at.isoformat(),
            }
            for inv in rows
        ],
    }
