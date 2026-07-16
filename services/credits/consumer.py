"""
RabbitMQ consumer for the credits service.

Consumes from Billing's `billing_events` exchange on the durable queue
`credits.billing_events` — the SAME queue name (and bindings) Billing
pre-declares at publish time, so events emitted before this service ever
started are waiting for us, not lost:

  invoice.paid   -> grant the account the plan's credits (purchase_grant row)
  refund.issued  -> claw back the matching credit grant (refund_clawback row)

Idempotency (spec §7 — consumers must survive at-least-once redelivery),
two independent layers:
  1. `already_processed(db, event_id)` + the processed_events table dedupe
     broker redeliveries: the event row and the ledger rows commit in the
     SAME transaction, so either both exist or neither does. A redelivery
     can therefore never double-credit.
  2. A business-key guard on the ledger itself (stripe_invoice_id on grants,
     stripe_refund_id on claw-backs) — covers the producer re-emitting the
     same invoice/refund under a FRESH event_id.

The consumer runs as a daemon thread (see main.py); the loop in `run()`
reconnects forever so a broker restart doesn't kill the service. A raising
handler makes the shared consumer retry (bounded) and then dead-letter.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from creditflow_common import rabbitmq
from creditflow_common.idempotency import already_processed

import database
import events
import ledger
from models import LedgerEntry

logger = logging.getLogger("credits.consumer")

EXCHANGE = "billing_events"          # Billing's exchange — we consume, never publish here
QUEUE = "credits.billing_events"     # matches Billing's pre-declared queue (events.py there)
ROUTING_KEYS = ["invoice.paid", "refund.issued"]


def handle_event(routing_key: str, data: dict, event_id: str) -> None:
    db = database.SessionLocal()
    try:
        if already_processed(db, event_id):
            db.commit()
            logger.info("skipping already-processed event %s (%s)", event_id, routing_key)
            return
        published: list[tuple[str, dict]] = []
        if routing_key == "invoice.paid":
            _grant_for_invoice(db, data, published)
        elif routing_key == "refund.issued":
            _claw_back_refund(db, data, published)
        db.commit()  # processed_events row + ledger rows land atomically
        # Publish only after the commit so we never announce a rolled-back write.
        for rk, payload in published:
            events.publish(rk, payload)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _grant_for_invoice(db: Session, data: dict, published: list[tuple[str, dict]]) -> None:
    account_id = data.get("account_id")
    stripe_invoice_id = data.get("stripe_invoice_id")
    plan = data.get("plan")
    if not account_id or not stripe_invoice_id:
        logger.warning("invoice.paid without account_id/stripe_invoice_id — dropping: %s", data)
        return

    # Business-key guard: one grant per Stripe invoice, ever.
    existing = db.scalar(select(LedgerEntry).where(
        LedgerEntry.entry_type == "purchase_grant",
        LedgerEntry.stripe_ref == stripe_invoice_id,
    ))
    if existing is not None:
        logger.info("invoice %s already granted (entry %s) — skipping", stripe_invoice_id, existing.id)
        return

    credits = ledger.PLAN_CREDITS.get(plan or "", 0)
    if credits <= 0:
        logger.info("plan %r grants no credits — ignoring invoice %s", plan, stripe_invoice_id)
        return

    before = ledger.balance(db, account_id)
    entry = LedgerEntry(
        account_id=account_id,
        amount=credits,
        entry_type="purchase_grant",
        stripe_ref=stripe_invoice_id,
        money_amount_cents=data.get("amount_paid"),
        reason=f"invoice.paid ({plan} plan)",
    )
    db.add(entry)
    db.flush()  # assigns entry.id for the event payload
    published.append(("credits.credited", {
        "account_id": account_id,
        "amount": credits,
        "balance": before + credits,
        "entry_type": "purchase_grant",
        "ledger_entry_id": entry.id,
        "stripe_ref": stripe_invoice_id,
        "plan": plan,
    }))
    logger.info("granted %d credits to %s for invoice %s", credits, account_id, stripe_invoice_id)


def _claw_back_refund(db: Session, data: dict, published: list[tuple[str, dict]]) -> None:
    account_id = data.get("account_id")
    stripe_refund_id = data.get("stripe_refund_id")
    refund_cents = data.get("amount")
    if not account_id or not stripe_refund_id:
        logger.warning("refund.issued without account_id/stripe_refund_id — dropping: %s", data)
        return

    # Business-key guard: one claw-back per Stripe refund, ever.
    existing = db.scalar(select(LedgerEntry).where(
        LedgerEntry.entry_type == "refund_clawback",
        LedgerEntry.stripe_ref == stripe_refund_id,
    ))
    if existing is not None:
        logger.info("refund %s already clawed back (entry %s) — skipping", stripe_refund_id, existing.id)
        return

    grant = _find_grant_to_reverse(db, account_id, refund_cents)
    if grant is None:
        # Nothing to reverse (e.g. refund of a free-plan invoice, or a grant
        # already clawed back). Ack and move on — dead-lettering would just
        # park an event no retry can ever satisfy.
        logger.warning("refund %s for %s matches no open credit grant — nothing to claw back",
                       stripe_refund_id, account_id)
        return

    before = ledger.balance(db, account_id)
    # The claw-back may push the balance NEGATIVE if the credits were already
    # spent — deliberate: the debt shows in the ledger and future grants pay
    # it down first, rather than the account keeping value it was refunded for.
    entry = LedgerEntry(
        account_id=account_id,
        amount=-grant.amount,
        entry_type="refund_clawback",
        stripe_ref=stripe_refund_id,
        related_entry_id=grant.id,
        money_amount_cents=refund_cents,
        reason=data.get("reason") or "refund.issued",
    )
    db.add(entry)
    db.flush()
    after = before - grant.amount
    published.append(("credits.debited", {
        "account_id": account_id,
        "amount": grant.amount,
        "balance": after,
        "entry_type": "refund_clawback",
        "ledger_entry_id": entry.id,
        "stripe_ref": stripe_refund_id,
        "reversed_entry_id": grant.id,
    }))
    if ledger.crossed_low_balance(before, after):
        published.append(("credits.low_balance", {
            "account_id": account_id,
            "balance": after,
            "threshold": ledger.LOW_BALANCE_THRESHOLD,
        }))
    logger.info("clawed back %d credits from %s for refund %s (grant %s)",
                grant.amount, account_id, stripe_refund_id, grant.id)


def _find_grant_to_reverse(db: Session, account_id: str, refund_cents: int | None) -> LedgerEntry | None:
    """Pick the grant a refund reverses. Billing's refund.issued carries the
    payment intent, not the Stripe invoice id, so there is no direct key into
    the grant rows — we match on the money amount when possible, else fall
    back to the most recent grant. Grants already reversed (referenced by a
    refund_clawback's related_entry_id) are excluded, so a grant can only be
    clawed back once."""
    reversed_ids = select(LedgerEntry.related_entry_id).where(
        LedgerEntry.entry_type == "refund_clawback",
        LedgerEntry.related_entry_id.is_not(None),
    )
    open_grants = (
        select(LedgerEntry)
        .where(
            LedgerEntry.account_id == account_id,
            LedgerEntry.entry_type == "purchase_grant",
            LedgerEntry.id.not_in(reversed_ids),
        )
        .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
    )
    if refund_cents:
        exact = db.scalars(open_grants.where(LedgerEntry.money_amount_cents == refund_cents)).first()
        if exact is not None:
            return exact
    return db.scalars(open_grants).first()


def run() -> None:
    """Blocking consume loop with reconnect — target for the daemon thread."""
    while True:
        try:
            rabbitmq.consume(
                exchange=EXCHANGE,
                queue=QUEUE,
                routing_keys=ROUTING_KEYS,
                handler=handle_event,
            )
        except Exception:  # noqa: BLE001 — broker hiccup: log, back off, reconnect
            logger.exception("consumer connection lost — retrying in 5s")
            time.sleep(5)
