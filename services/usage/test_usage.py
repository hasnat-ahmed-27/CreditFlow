"""
Usage service tests: the quota pre-check passes/denies off the Redis counter,
ai.generation_completed appends to the usage_ledger and updates the counter,
the consumer is idempotent (event_id replay AND fresh-event_id/same-job
replay), usage.threshold_reached fires exactly on the 80%/100% crossings,
a lost or drifted Redis counter reconciles back to the Postgres truth, and
the summary endpoint aggregates tokens/cost by model per account.

No infra: SQLite via conftest, consumer.handle_event called directly (the
exact function the broker would call), fakeredis, publisher stubbed.

Quota in tests: conftest pins USAGE_QUOTA_TOKENS=1000 -> lines at 800/1000.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from creditflow_common import jwt_utils
from creditflow_common.idempotency import ProcessedEvent

import consumer
import quota
import store
from models import UsageEntry

QUOTA = 1000  # mirrors conftest's USAGE_QUOTA_TOKENS


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(account_id: str, role: str = "member", user_id: str | None = None) -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues."""
    token, _ = jwt_utils.sign_access_token(user_id or _uid(), account_id, role)
    return {"Authorization": f"Bearer {token}"}


def _generation_completed(account_id: str, tokens: int = 100, model: str = "openai/gpt-4o-mini",
                          cost_usd: float = 0.0025, job_id: str | None = None) -> tuple[dict, str]:
    """(payload, event_id) shaped exactly like the AI service will emit it."""
    return {
        "account_id": account_id,
        "user_id": _uid(),
        "job_id": job_id or f"job_{uuid.uuid4().hex[:12]}",
        "model": model,
        "input_tokens": tokens // 2,
        "output_tokens": tokens - tokens // 2,
        "total_tokens": tokens,
        "cost_usd": cost_usd,
    }, f"evt_{uuid.uuid4().hex}"


def _consume(account_id: str, tokens: int, **kwargs) -> tuple[dict, str]:
    payload, event_id = _generation_completed(account_id, tokens=tokens, **kwargs)
    consumer.handle_event("ai.generation_completed", payload, event_id)
    return payload, event_id


def _check(client, account_id: str, estimated: int = 1) -> dict:
    r = client.post("/usage/check", json={"estimated_tokens": estimated}, headers=_auth(account_id))
    assert r.status_code == 200, r.text
    return r.json()


def _entries(db, account_id: str) -> list[UsageEntry]:
    db.expire_all()
    return db.scalars(select(UsageEntry).where(UsageEntry.account_id == account_id)
                      .order_by(UsageEntry.created_at, UsageEntry.id)).all()


# --------------------------------------------------------------------------
# Quota pre-check (the endpoint the AI service calls)
# --------------------------------------------------------------------------

def test_precheck_allows_fresh_account(client):
    account_id = _uid()
    body = _check(client, account_id, estimated=500)
    assert body["allowed"] is True
    assert body["used_tokens"] == 0
    assert body["quota_tokens"] == QUOTA
    assert body["remaining_tokens"] == QUOTA


def test_precheck_denies_when_estimate_exceeds_remaining(client):
    account_id = _uid()
    _consume(account_id, tokens=950)
    body = _check(client, account_id, estimated=100)   # 950 + 100 > 1000
    assert body["allowed"] is False
    assert body["used_tokens"] == 950
    assert body["remaining_tokens"] == 50
    # ...but a request that still fits is allowed.
    assert _check(client, account_id, estimated=50)["allowed"] is True


def test_precheck_requires_auth(client):
    assert client.post("/usage/check", json={}).status_code == 401


# --------------------------------------------------------------------------
# Consumer: ai.generation_completed -> usage_ledger row + Redis counter
# --------------------------------------------------------------------------

def test_generation_completed_records_usage(client, db_session):
    account_id = _uid()
    payload, event_id = _consume(account_id, tokens=300, model="openai/gpt-4o", cost_usd=0.0123)

    entries = _entries(db_session, account_id)
    assert len(entries) == 1
    e = entries[0]
    assert e.job_id == payload["job_id"]
    assert e.model == "openai/gpt-4o"
    assert e.total_tokens == 300
    assert e.input_tokens + e.output_tokens == 300
    assert Decimal(str(e.cost_usd)) == Decimal("0.0123")
    assert e.period == quota.current_period()
    # processed_events recorded the event_id (spec §7)
    assert db_session.get(ProcessedEvent, event_id) is not None
    # Redis counter reconciled to the ledger sum on write
    assert store.get_tokens(account_id, quota.current_period()) == 300


def test_generation_without_account_or_job_is_dropped(client, db_session):
    """Malformed events ack (recorded in processed_events) but write nothing —
    dead-lettering them would park events no retry can fix."""
    payload, event_id = _generation_completed(_uid(), tokens=100)
    del payload["job_id"]
    account_id = payload["account_id"]
    consumer.handle_event("ai.generation_completed", payload, event_id)
    assert _entries(db_session, account_id) == []
    assert db_session.get(ProcessedEvent, event_id) is not None


# --------------------------------------------------------------------------
# Idempotency (spec §7): redelivery and producer re-emit
# --------------------------------------------------------------------------

def test_consumer_idempotent_on_event_redelivery(client, db_session):
    """Same event delivered twice (broker redelivery) -> counted ONCE."""
    account_id = _uid()
    payload, event_id = _consume(account_id, tokens=400)
    consumer.handle_event("ai.generation_completed", payload, event_id)  # redelivery

    assert len(_entries(db_session, account_id)) == 1
    assert _check(client, account_id)["used_tokens"] == 400


def test_consumer_idempotent_on_fresh_event_id_same_job(client, db_session):
    """AI service re-emits the SAME job under a NEW event_id -> the
    business-key guard (job_id) still prevents double-counting."""
    account_id = _uid()
    payload, _ = _consume(account_id, tokens=400)
    consumer.handle_event("ai.generation_completed", payload, f"evt_{uuid.uuid4().hex}")

    assert len(_entries(db_session, account_id)) == 1
    assert _check(client, account_id)["used_tokens"] == 400


def test_redelivery_does_not_reemit_threshold_events(client, published_events):
    account_id = _uid()
    payload, event_id = _consume(account_id, tokens=900)   # crosses 80%
    assert len(published_events) == 1
    consumer.handle_event("ai.generation_completed", payload, event_id)  # redelivery
    assert len(published_events) == 1                       # not announced again


# --------------------------------------------------------------------------
# usage.threshold_reached at 80% / 100% crossings
# --------------------------------------------------------------------------

def test_threshold_80_emitted_on_crossing(client, published_events):
    account_id = _uid()
    _consume(account_id, tokens=700)                       # below the line
    assert published_events == []
    _consume(account_id, tokens=150)                       # 700 -> 850 crosses 800

    assert [rk for rk, _ in published_events] == ["usage.threshold_reached"]
    evt = published_events[0][1]
    assert evt["threshold_percent"] == 80
    assert evt["account_id"] == account_id
    assert evt["used_tokens"] == 850
    assert evt["quota_tokens"] == QUOTA
    assert evt["period"] == quota.current_period()


def test_threshold_100_emitted_on_crossing(client, published_events):
    account_id = _uid()
    _consume(account_id, tokens=900)                       # crosses 80
    _consume(account_id, tokens=150)                       # 900 -> 1050 crosses 100

    thresholds = [p["threshold_percent"] for rk, p in published_events
                  if rk == "usage.threshold_reached"]
    assert thresholds == [80, 100]


def test_both_thresholds_in_one_event(client, published_events):
    """A single big generation can cross 80 AND 100 at once."""
    account_id = _uid()
    _consume(account_id, tokens=1200)
    thresholds = [p["threshold_percent"] for _, p in published_events]
    assert thresholds == [80, 100]


def test_no_reemission_while_already_above_threshold(client, published_events):
    account_id = _uid()
    _consume(account_id, tokens=850)                       # crosses 80
    published_events.clear()
    _consume(account_id, tokens=50)                        # 850 -> 900: no new crossing
    assert published_events == []


# --------------------------------------------------------------------------
# Redis <-> Postgres reconciliation
# --------------------------------------------------------------------------

def test_counter_rebuilt_from_ledger_on_redis_miss(client):
    """Redis flush/restart loses the counter -> the next pre-check rebuilds
    it from the durable ledger instead of answering from nothing."""
    account_id = _uid()
    period = quota.current_period()
    _consume(account_id, tokens=600)
    store.get_redis().flushall()                           # simulate Redis dying
    assert store.get_tokens(account_id, period) is None

    assert _check(client, account_id)["used_tokens"] == 600   # truth survived
    assert store.get_tokens(account_id, period) == 600        # counter restored


def test_write_reconciles_drifted_counter(client):
    """A drifted counter (bug, partial failure) is overwritten with the
    Postgres-derived sum on the next ledger write — drift never accumulates."""
    account_id = _uid()
    period = quota.current_period()
    _consume(account_id, tokens=200)
    store.set_tokens(account_id, period, 999_999)          # inject drift
    _consume(account_id, tokens=100)

    assert store.get_tokens(account_id, period) == 300     # ledger truth, not 1_000_099
    assert _check(client, account_id)["used_tokens"] == 300


def test_redelivery_skip_path_also_reconciles(client):
    """Crash window: ledger committed but Redis never written. On redelivery
    the skip path still reconciles the counter to the committed truth."""
    account_id = _uid()
    period = quota.current_period()
    payload, event_id = _consume(account_id, tokens=500)
    store.get_redis().flushall()                           # counter lost post-commit
    consumer.handle_event("ai.generation_completed", payload, event_id)  # redelivery
    assert store.get_tokens(account_id, period) == 500


# --------------------------------------------------------------------------
# Usage summary (Admin dashboard)
# --------------------------------------------------------------------------

def test_summary_aggregates_by_model(client):
    account_id = _uid()
    _consume(account_id, tokens=300, model="openai/gpt-4o", cost_usd=0.03)
    _consume(account_id, tokens=200, model="openai/gpt-4o", cost_usd=0.02)
    _consume(account_id, tokens=100, model="meta-llama/llama-3-8b", cost_usd=0.001)

    r = client.get("/usage/summary", headers=_auth(account_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["used_tokens"] == 600
    assert body["total_generations"] == 3
    assert body["total_cost_usd"] == 0.051
    assert body["quota_tokens"] == QUOTA
    assert body["used_percent"] == 60.0
    assert [(m["model"], m["total_tokens"], m["generations"]) for m in body["by_model"]] == [
        ("openai/gpt-4o", 500, 2),
        ("meta-llama/llama-3-8b", 100, 1),
    ]
    assert body["by_model"][0]["cost_usd"] == 0.05


def test_summary_scoped_to_token_account(client):
    """Another account's usage never leaks into the summary (spec §6)."""
    account_a, account_b = _uid(), _uid()
    _consume(account_a, tokens=300)
    _consume(account_b, tokens=999)

    body = client.get("/usage/summary", headers=_auth(account_a)).json()
    assert body["used_tokens"] == 300
    assert body["total_generations"] == 1


def test_summary_requires_auth(client):
    assert client.get("/usage/summary").status_code == 401
