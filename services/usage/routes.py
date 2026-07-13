"""
Usage endpoints: quota pre-check (hot path, Redis) and the per-account usage
summary (Admin dashboard, Postgres).

Authorization model (same as Credits): identity comes from the RS256 access
token, verified with the shared public key via creditflow_common.jwt_utils.
Everything is scoped by the token's account_id claim — spec §6: usage is
account-level data. Any member may hit both endpoints: the pre-check is
called on behalf of whichever member is generating, and the summary backs
the dashboard every member can see.

The pre-check deliberately returns 200 with an `allowed` flag rather than an
error status: "quota exhausted" is a normal answer the AI service branches
on, not a failure of the check itself.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creditflow_common import jwt_utils

import database
import quota
import schemas
from models import UsageEntry

router = APIRouter(tags=["usage"])


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


@router.post("/usage/check")
def quota_check(
    body: schemas.QuotaCheckRequest,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """The gate the AI service calls before every generation. One Redis GET
    on the happy path; a Redis miss falls back to the ledger sum and rewrites
    the counter (reconcile-on-read), so a flushed Redis costs one slow check,
    never a wrong verdict."""
    account_id = claims["account_id"]
    period = quota.current_period()
    account_quota = quota.quota_for(account_id)
    used = quota.tokens_used(db, account_id, period)
    remaining = max(account_quota - used, 0)
    allowed = account_quota <= 0 or used + body.estimated_tokens <= account_quota
    return {
        "account_id": account_id,
        "period": period,
        "allowed": allowed,
        "used_tokens": used,
        "quota_tokens": account_quota,
        "remaining_tokens": remaining,
    }


@router.get("/usage/summary")
def usage_summary(
    period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Per-account rollup for the Admin dashboard: tokens used, cost, and a
    by-model breakdown — aggregated from the durable ledger (the truth), not
    the Redis counter. Defaults to the current period."""
    account_id = claims["account_id"]
    period = period or quota.current_period()
    rows = db.execute(
        select(
            UsageEntry.model,
            func.count(UsageEntry.id),
            func.coalesce(func.sum(UsageEntry.input_tokens), 0),
            func.coalesce(func.sum(UsageEntry.output_tokens), 0),
            func.coalesce(func.sum(UsageEntry.total_tokens), 0),
            func.coalesce(func.sum(UsageEntry.cost_usd), 0),
        )
        .where(UsageEntry.account_id == account_id, UsageEntry.period == period)
        .group_by(UsageEntry.model)
        .order_by(func.sum(UsageEntry.total_tokens).desc())
    ).all()

    by_model = [
        {
            "model": model,
            "generations": count,
            "input_tokens": int(inp),
            "output_tokens": int(out),
            "total_tokens": int(total),
            "cost_usd": float(cost),
        }
        for model, count, inp, out, total, cost in rows
    ]
    used = sum(m["total_tokens"] for m in by_model)
    account_quota = quota.quota_for(account_id)
    return {
        "account_id": account_id,
        "period": period,
        "quota_tokens": account_quota,
        "used_tokens": used,
        "used_percent": round(used * 100 / account_quota, 2) if account_quota > 0 else None,
        "total_generations": sum(m["generations"] for m in by_model),
        "total_cost_usd": round(sum(m["cost_usd"] for m in by_model), 6),
        "by_model": by_model,
    }
