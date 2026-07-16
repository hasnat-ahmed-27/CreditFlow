"""
Ledger arithmetic — the ONLY place balances come from.

`balance()` derives the number by summing credits_ledger rows; nothing in
this service stores a balance anywhere else (see models.py for why).
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creditflow_common import config

from models import LedgerEntry

# invoice.paid grants per plan (Billing's invoice.paid payload carries the
# plan). Free has no invoice, so it never appears here.
PLAN_CREDITS: dict[str, int] = {
    "pro": int(config.env("CREDITS_PLAN_PRO", "1000")),
    "team": int(config.env("CREDITS_PLAN_TEAM", "5000")),
}

LOW_BALANCE_THRESHOLD = int(config.env("CREDITS_LOW_BALANCE_THRESHOLD", "100"))


def balance(db: Session, account_id: str) -> int:
    """SUM over the append-only ledger — never a stored counter."""
    return int(db.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0))
        .where(LedgerEntry.account_id == account_id)
    ))


def crossed_low_balance(before: int, after: int) -> bool:
    """Emit credits.low_balance only on the downward CROSSING of the
    threshold, not on every debit while already below it — otherwise every
    small spend after the first alert would re-notify."""
    return before >= LOW_BALANCE_THRESHOLD > after
