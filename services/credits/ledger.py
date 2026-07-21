"""
Ledger arithmetic — the ONLY place balances come from.

`balance()` derives the number by summing credits_ledger rows; nothing in
this service stores a balance anywhere else (see models.py for why).
"""
from __future__ import annotations

import math

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

# --- AI generation pricing (spec §10: "deducts credits correctly") ---------
#
# THE FORMULA:  credits = ceil(total_tokens / 1000) * CREDITS_PER_1K_TOKENS
#
# Priced per 1K TOKENS, not per provider dollar, for three reasons:
#   - it is the unit the rest of the platform already meters in (the Usage
#     service's quota is a token quota, and ai.generation_completed always
#     carries token counts — `cost_usd` is None whenever the numbers are our
#     own estimate, e.g. a cancelled stream);
#   - credits are a product currency the user buys in round packs, so their
#     price must not move when a provider changes its rate card;
#   - it is deterministic, which makes the debit reproducible from the event
#     payload alone — the property the idempotency guard depends on.
#
# Rounding is UP, and any metered generation costs at least one credit: a
# thousand 10-token generations must not be free. Integers throughout — the
# ledger stores whole credits (models.py), so there is no fractional balance
# to drift.
CREDITS_PER_1K_TOKENS = int(config.env("CREDITS_PER_1K_TOKENS", "1"))
TOKENS_PER_CREDIT_UNIT = 1000


def credits_for_tokens(total_tokens: int) -> int:
    """Credits owed for a completed generation. Zero tokens costs nothing
    (a failed/empty generation is not a sale)."""
    if total_tokens <= 0:
        return 0
    units = math.ceil(total_tokens / TOKENS_PER_CREDIT_UNIT)
    return max(1, units * CREDITS_PER_1K_TOKENS) if CREDITS_PER_1K_TOKENS > 0 else 0


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


def went_negative(before: int, after: int) -> bool:
    """The moment a balance first goes into debt. Only the two debits that
    may legitimately overdraw reach this — a refund claw-back of already-spent
    credits, and an AI generation whose tokens were consumed before we could
    charge for them. Both are already-happened facts, so the ledger records
    the debt rather than pretending it isn't owed; this is the flag that says
    "tell the account", even when the balance was ALREADY below the low
    threshold and crossed_low_balance would stay quiet."""
    return before >= 0 > after
