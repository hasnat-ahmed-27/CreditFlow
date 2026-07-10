"""
stripe_gateway.py — the only module that talks to the Stripe SDK.

Everything Stripe is funneled through here so (a) tests can mock the SDK in
one place with zero network calls, and (b) the test-mode-only rule is
enforced at startup: this service refuses to boot with a live-mode secret
key. Plan tiers map to Stripe Price ids via env (created once in the Stripe
test dashboard).
"""
from __future__ import annotations

import stripe

from creditflow_common import config

STRIPE_SECRET_KEY = config.env("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = config.env("STRIPE_WEBHOOK_SECRET", "")

# Paid tiers only — "free" is the absence of a Stripe subscription.
PRICE_IDS: dict[str, str] = {
    "pro": config.env("STRIPE_PRICE_PRO", "price_test_pro"),
    "team": config.env("STRIPE_PRICE_TEAM", "price_test_team"),
}
PLAN_BY_PRICE = {price: plan for plan, price in PRICE_IDS.items()}

CHECKOUT_SUCCESS_URL = config.env(
    "BILLING_CHECKOUT_SUCCESS_URL",
    "http://localhost:3000/billing/success?session_id={CHECKOUT_SESSION_ID}",
)
CHECKOUT_CANCEL_URL = config.env("BILLING_CHECKOUT_CANCEL_URL", "http://localhost:3000/billing/cancel")


def init() -> None:
    """Called from the FastAPI lifespan. Hard-fails on a live-mode key —
    the spec allows sandbox/test keys only."""
    if STRIPE_SECRET_KEY and not STRIPE_SECRET_KEY.startswith("sk_test_"):
        raise RuntimeError("Billing runs with Stripe TEST-mode keys only (sk_test_...)")
    stripe.api_key = STRIPE_SECRET_KEY


def create_customer(account_id: str) -> dict:
    """One Stripe Customer per tenant; metadata carries our account_id so the
    linkage is visible from the Stripe dashboard too."""
    return stripe.Customer.create(metadata={"account_id": account_id})


def create_checkout_session(customer_id: str, plan: str, account_id: str) -> dict:
    """Hosted Checkout in subscription mode. metadata/client_reference_id let
    the checkout.session.completed webhook join back to the account."""
    return stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": PRICE_IDS[plan], "quantity": 1}],
        client_reference_id=account_id,
        metadata={"account_id": account_id, "plan": plan},
        subscription_data={"metadata": {"account_id": account_id, "plan": plan}},
        success_url=CHECKOUT_SUCCESS_URL,
        cancel_url=CHECKOUT_CANCEL_URL,
    )


def change_plan(stripe_subscription_id: str, plan: str) -> dict:
    """Upgrade/downgrade between paid tiers by swapping the subscription
    item's price. proration_behavior='create_prorations' is the spec's
    'with Stripe proration': the next invoice carries the credit/charge for
    the partial period."""
    sub = stripe.Subscription.retrieve(stripe_subscription_id)
    item_id = sub["items"]["data"][0]["id"]
    return stripe.Subscription.modify(
        stripe_subscription_id,
        items=[{"id": item_id, "price": PRICE_IDS[plan]}],
        proration_behavior="create_prorations",
        metadata={"plan": plan},
    )


def cancel_subscription(stripe_subscription_id: str) -> dict:
    """Immediate cancel with a final prorated invoice (downgrade-to-free)."""
    return stripe.Subscription.cancel(stripe_subscription_id, invoice_now=True, prorate=True)


def create_refund(payment_intent_id: str, amount: int | None = None, reason: str | None = None) -> dict:
    """amount=None refunds the full charge; amount is in cents otherwise."""
    kwargs: dict = {"payment_intent": payment_intent_id}
    if amount is not None:
        kwargs["amount"] = amount
    if reason is not None:
        kwargs["reason"] = reason
    return stripe.Refund.create(**kwargs)


def construct_webhook_event(payload: bytes, sig_header: str) -> dict:
    """Verify the Stripe-Signature header (HMAC over timestamp.payload with
    the endpoint secret) and parse the event. Raises on any tamper/replay."""
    return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
