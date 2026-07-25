"""
Core credits endpoints: balance, transaction history, and consumption.

Authorization model (same as Billing): identity comes from the RS256 access
token, verified with the shared public key via creditflow_common.jwt_utils.
Everything is scoped by the token's account_id claim — spec §6: credit
balance is shared at the ACCOUNT level, so any member may read the balance
and spend credits (AI usage is a member activity). Marketplace endpoints
(selling/buying account value) are owner-only — see marketplace.py.

Write-path rule: the ledger row commits first; events publish AFTER the
commit so we never announce a rolled-back write. A debit that would take the
balance negative is rejected inside the same session that computed the
balance — the ledger itself never goes below zero via spending (only a
refund claw-back may push it negative; see consumer.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from creditflow_common import jwt_utils

import database
import events
import ledger
import schemas
from models import LedgerEntry

router = APIRouter(tags=["credits"])


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


def entry_view(e: LedgerEntry) -> dict:
    return {
        "id": e.id,
        "amount": e.amount,
        "entry_type": e.entry_type,
        "stripe_ref": e.stripe_ref,
        "job_id": e.job_id,
        "listing_id": e.listing_id,
        "counterparty_account_id": e.counterparty_account_id,
        "money_amount_cents": e.money_amount_cents,
        "reason": e.reason,
        "created_by_user_id": e.created_by_user_id,
        "created_at": e.created_at.isoformat(),
    }


def emit_balance_events(account_id: str, entry: LedgerEntry, before: int, after: int) -> None:
    """Post-commit event fan-out for one ledger row: credited/debited always,
    low_balance only on the downward threshold crossing."""
    rk = "credits.credited" if entry.amount > 0 else "credits.debited"
    events.publish(rk, {
        "account_id": account_id,
        "amount": abs(entry.amount),
        "balance": after,
        "entry_type": entry.entry_type,
        "ledger_entry_id": entry.id,
        "listing_id": entry.listing_id,
        "stripe_ref": entry.stripe_ref,
        "reason": entry.reason,
    })
    if ledger.crossed_low_balance(before, after):
        events.publish("credits.low_balance", {
            "account_id": account_id,
            "balance": after,
            "threshold": ledger.LOW_BALANCE_THRESHOLD,
        })


@router.get("/credits/balance")
def get_balance(claims: dict = Depends(current_claims), db: Session = Depends(database.get_db)) -> dict:
    account_id = claims["account_id"]
    return {
        "account_id": account_id,
        "balance": ledger.balance(db, account_id),
        "low_balance_threshold": ledger.LOW_BALANCE_THRESHOLD,
    }


@router.get("/credits/history")
def transaction_history(
    limit: int = 100,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    account_id = claims["account_id"]
    rows = db.scalars(
        select(LedgerEntry)
        .where(LedgerEntry.account_id == account_id)
        .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
        .limit(min(max(limit, 1), 500))
    ).all()
    return {
        "account_id": account_id,
        "balance": ledger.balance(db, account_id),
        "entries": [entry_view(e) for e in rows],
    }


@router.post("/credits/consume", status_code=201)
def consume_credits(
    body: schemas.ConsumeRequest,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Debit credits for ad-hoc usage. Any member — usage is the whole point
    of the shared account balance.

    NOT the AI path: AI generations are debited by the consumer, off
    `ai.generation_completed` (see consumer.py), so nothing needs to call
    this after a generation — doing so would charge the account twice.
    Unlike that path this one refuses to overdraw, because a caller-initiated
    spend can still be told "no" before it happens."""
    account_id = claims["account_id"]
    before = ledger.balance(db, account_id)
    if body.amount > before:
        raise HTTPException(status_code=409, detail=f"Insufficient credits: balance {before}, requested {body.amount}")
    entry = LedgerEntry(
        account_id=account_id,
        amount=-body.amount,
        entry_type="usage_debit",
        reason=body.reason,
        created_by_user_id=claims["sub"],
    )
    db.add(entry)
    db.commit()
    after = before - body.amount
    emit_balance_events(account_id, entry, before, after)
    return {"ledger_entry_id": entry.id, "account_id": account_id, "debited": body.amount, "balance": after}
