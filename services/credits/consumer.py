"""
RabbitMQ consumers for the credits service.

QUEUE CONTRACT — we consume the durable queues our producers pre-declare at
publish time, so events emitted before this service ever started are waiting
for us, not lost (same contract every service here uses). One consumer
thread per queue (the shared pika loop is blocking), both funnelling into the
ONE handle_event below — tests call it directly, exactly like the other
services.

  queue                     exchange        keys            what we do
  credits.billing_events    billing_events  invoice.paid    grant the plan's
                                                            credits
                                            refund.issued   claw the grant back
  credits.usage_events      usage_events    ai.generation_  debit the account
                                            completed       for the tokens spent

WHY CREDITS CONSUMES ai.generation_completed rather than the AI service
calling a debit endpoint (spec §10: "an AI generation … deducts credits
correctly"):
  - The spec's event contract for Service 7 is "Publishes:
    ai.generation_completed / ai.generation_failed, Consumes: none". A
    synchronous debit call would put Credits inside the AI request path, so a
    Credits outage would fail generations that already streamed and were
    already billed by the provider — money spent, nothing recorded.
  - The event already carries everything a debit needs (account_id, job_id,
    token counts) and is already published reliably (durable, confirms,
    DLX-backed). Usage and Content are ALREADY consumers of it; Credits
    simply joins the fan-out on its own queue, which is the pattern the
    platform is built on.
  - The debit therefore cannot be lost: if this service is down the event
    waits in `credits.usage_events` and is applied when it comes back.

Idempotency (spec §7 — consumers must survive at-least-once redelivery),
two independent layers:
  1. `already_processed(db, event_id)` + the processed_events table dedupe
     broker redeliveries: the event row and the ledger rows commit in the
     SAME transaction, so either both exist or neither does. A redelivery
     can therefore never double-credit or double-DEBIT.
  2. A business-key guard on the ledger itself (stripe_invoice_id on grants,
     stripe_refund_id on claw-backs, job_id on generation debits) — covers
     the producer re-emitting the same invoice/refund/generation under a
     FRESH event_id.

The consumer threads are daemons (see main.py); each loop in `_run_binding`
reconnects forever so a broker restart doesn't kill the service. A raising
handler makes the shared consumer retry (bounded) and then dead-letter.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from creditflow_common import rabbitmq
from creditflow_common.idempotency import already_processed

import database
import events
import ledger
from models import LedgerEntry

logger = logging.getLogger("credits.consumer")


@dataclass(frozen=True)
class Binding:
    exchange: str
    queue: str
    routing_keys: tuple[str, ...]


BINDINGS: tuple[Binding, ...] = (
    # Billing's exchange and its pre-declared queue (events.py there).
    Binding("billing_events", "credits.billing_events",
            ("invoice.paid", "refund.issued")),
    # The AI service's domain exchange. Our own queue, declared on connect —
    # bindings are additive, so joining the existing ai.generation_completed
    # fan-out (Usage, Content) changes nothing for those consumers.
    Binding("usage_events", "credits.usage_events",
            ("ai.generation_completed",)),
)


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
        elif routing_key == "ai.generation_completed":
            _debit_for_generation(db, data, published)
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


def _debit_for_generation(db: Session, data: dict, published: list[tuple[str, dict]]) -> None:
    """ai.generation_completed -> one generation_debit row (spec §10).

    INSUFFICIENT BALANCE: the debit still lands, and the balance may go
    NEGATIVE — the same deliberate choice refund claw-backs make. By the time
    this event exists the tokens have already been streamed and the provider
    has already charged us; refusing to record the debit would not un-spend
    them, it would just hide the debt and let the account keep generating for
    free. Overspend is PREVENTED up front by the AI service's synchronous
    quota gate against Usage (services/ai/usage_client.py), which is the
    correct place for a "no" — before any money moves. Here we only record
    what happened, and raise the alarm: credits.low_balance fires on the
    downward threshold crossing OR the moment the balance goes into debt,
    carrying `insufficient` so the Notification service can say so plainly.

    A cancelled generation still arrives here (the AI worker emits
    generation_completed with status="cancelled" and estimated token counts,
    because the provider billed the tokens it streamed) and is charged the
    same way — it consumed real capacity.
    """
    account_id = data.get("account_id")
    job_id = data.get("job_id")
    if not account_id or not job_id:
        logger.warning("ai.generation_completed without account_id/job_id — dropping: %s", data)
        return

    # Business-key guard: one debit per generation job, ever.
    existing = db.scalar(select(LedgerEntry).where(
        LedgerEntry.entry_type == "generation_debit",
        LedgerEntry.job_id == job_id,
    ))
    if existing is not None:
        logger.info("job %s already debited (entry %s) — skipping", job_id, existing.id)
        return

    input_tokens = int(data.get("input_tokens") or 0)
    output_tokens = int(data.get("output_tokens") or 0)
    total_tokens = int(data.get("total_tokens") or (input_tokens + output_tokens))
    credits = ledger.credits_for_tokens(total_tokens)
    if credits <= 0:
        # A generation that produced nothing measurable is not a sale; writing
        # a zero row would violate the ledger's "never zero" invariant.
        logger.info("generation %s used %d tokens — nothing to debit", job_id, total_tokens)
        return

    before = ledger.balance(db, account_id)
    after = before - credits
    entry = LedgerEntry(
        account_id=account_id,
        amount=-credits,
        entry_type="generation_debit",
        job_id=job_id,
        reason=f"ai.generation_completed ({total_tokens} tokens, {data.get('model') or 'unknown'})",
        # Audit attribution (spec §6) — the member who ran the generation.
        created_by_user_id=data.get("user_id"),
    )
    db.add(entry)
    db.flush()  # assigns entry.id for the event payload
    published.append(("credits.debited", {
        "account_id": account_id,
        "amount": credits,
        "balance": after,
        "entry_type": "generation_debit",
        "ledger_entry_id": entry.id,
        "job_id": job_id,
        "total_tokens": total_tokens,
        "model": data.get("model"),
    }))
    if ledger.crossed_low_balance(before, after) or ledger.went_negative(before, after):
        published.append(("credits.low_balance", {
            "account_id": account_id,
            "balance": after,
            "threshold": ledger.LOW_BALANCE_THRESHOLD,
            "insufficient": after < 0,
        }))
        if after < 0:
            logger.warning("account %s overdrew on generation %s: balance %d",
                           account_id, job_id, after)
    logger.info("debited %d credits from %s for generation %s (%d tokens)",
                credits, account_id, job_id, total_tokens)


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


def _run_binding(binding: Binding) -> None:
    """Blocking consume loop with reconnect for ONE queue."""
    while True:
        try:
            rabbitmq.consume(
                exchange=binding.exchange,
                queue=binding.queue,
                routing_keys=list(binding.routing_keys),
                handler=handle_event,
            )
        except Exception:  # noqa: BLE001 — broker hiccup: log, back off, reconnect
            logger.exception("consumer for %s lost connection — retrying in 5s", binding.queue)
            time.sleep(5)


def run() -> None:
    """Start one daemon consumer thread per queue and wait on them — target
    for the single thread main.py spawns."""
    threads = []
    for binding in BINDINGS:
        t = threading.Thread(target=_run_binding, args=(binding,),
                             name=f"credits-consumer-{binding.queue}", daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
