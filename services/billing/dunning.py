"""
Dunning resolution (spec: "on payment_failed, start a grace-period timer;
emit subscription.downgraded if unresolved").

The timer STARTS in webhooks._on_invoice_payment_failed (status -> past_due,
grace_expires_at set once). It RESOLVES in one of two ways:
  - invoice.paid arrives during grace  -> back to active (webhooks.py), or
  - grace expires                      -> apply_due() downgrades to free.

apply_due() runs on every poller tick. Each downgrade is its own transaction
pairing the state change with its outbox.stage("subscription.downgraded") —
the same atomicity rule as everywhere else in this service.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

import outbox
import stripe_gateway
from models import Subscription, as_aware, utcnow

logger = logging.getLogger("billing.dunning")


def apply_due(db: Session) -> list[str]:
    """Downgrade every subscription whose grace period has expired.
    Idempotent: a downgraded row leaves past_due, so a second run (or a
    concurrent replica) finds nothing. Returns the affected account_ids."""
    now = utcnow()
    candidates = db.scalars(
        select(Subscription).where(
            Subscription.status == "past_due",
            Subscription.grace_expires_at.is_not(None),
        )
    ).all()

    downgraded: list[str] = []
    for sub in candidates:
        if as_aware(sub.grace_expires_at) > now:
            continue  # still inside grace
        # Best-effort Stripe cleanup first (outside the txn): stop the unpaid
        # subscription from generating more failed invoices. Stripe's own
        # dunning cancels it eventually anyway, so failure here is non-fatal.
        if sub.stripe_subscription_id:
            try:
                stripe_gateway.cancel_subscription(sub.stripe_subscription_id)
            except Exception:  # noqa: BLE001
                logger.warning("could not cancel stripe sub %s during dunning", sub.stripe_subscription_id)
        previous_plan = sub.plan
        sub.plan = "free"
        sub.status = "active"          # active on the free tier
        sub.stripe_subscription_id = None
        sub.grace_expires_at = None
        outbox.stage(db, "subscription.downgraded", {
            "account_id": sub.account_id,
            "previous_plan": previous_plan,
            "plan": "free",
            "reason": "dunning_grace_expired",
        })
        db.commit()  # state + outbox row, atomically, per subscription
        downgraded.append(sub.account_id)
        logger.info("dunning: downgraded account %s from %s", sub.account_id, previous_plan)
    return downgraded
