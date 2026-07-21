"""
Credits service tests: account.created opens a new account on its free-tier
starter grant (spec §4), invoice.paid grants credits, ai.generation_completed
debits them (spec §10), the consumer is idempotent (event_id replay AND
fresh-event_id/same-business-key replay), refund.issued claws back the
matching grant exactly once, the marketplace transfer is atomic (both ledger
rows or neither), the balance is always derived from the ledger,
over-balance debits/transfers are rejected, and credits.credited /
credits.debited / credits.low_balance are emitted.

No infra: SQLite via conftest, consumer.handle_event called directly (the
exact function the broker would call), publisher stubbed.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from creditflow_common import jwt_utils
from creditflow_common.idempotency import ProcessedEvent

import consumer
import database
import ledger
from models import LedgerEntry, MarketplaceListing


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(account_id: str, role: str = "owner", user_id: str | None = None) -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues."""
    token, _ = jwt_utils.sign_access_token(user_id or _uid(), account_id, role)
    return {"Authorization": f"Bearer {token}"}


def _invoice_paid(account_id: str, plan: str = "pro", amount_paid: int = 2900,
                  stripe_invoice_id: str | None = None, event_id: str | None = None) -> tuple[dict, str]:
    """(payload, event_id) shaped exactly like Billing's outbox emits it."""
    return {
        "account_id": account_id,
        "plan": plan,
        "stripe_invoice_id": stripe_invoice_id or f"in_{uuid.uuid4().hex[:8]}",
        "amount_paid": amount_paid,
        "currency": "usd",
        "dunning_recovered": False,
    }, event_id or f"evt_{uuid.uuid4().hex}"


def _refund_issued(account_id: str, amount: int = 2900, stripe_refund_id: str | None = None) -> tuple[dict, str]:
    return {
        "account_id": account_id,
        "refund_id": _uid(),
        "stripe_refund_id": stripe_refund_id or f"re_{uuid.uuid4().hex[:8]}",
        "stripe_payment_intent_id": f"pi_{uuid.uuid4().hex[:8]}",
        "invoice_id": _uid(),
        "amount": amount,
        "currency": "usd",
        "reason": "requested_by_customer",
    }, f"evt_{uuid.uuid4().hex}"


def _account_created(account_id: str, type_: str = "individual", plan_tier: str = "free",
                     owner_user_id: str | None = None,
                     event_id: str | None = None) -> tuple[dict, str]:
    """(payload, event_id) shaped exactly like the User service emits it
    (services/user/provisioning.py:account_created_event)."""
    return {
        "account_id": account_id,
        "type": type_,
        "name": "acme@example.com",
        "plan_tier": plan_tier,
        "owner_user_id": owner_user_id or _uid(),
    }, event_id or f"evt_{uuid.uuid4().hex}"


def _generation_completed(account_id: str, total_tokens: int = 2500, job_id: str | None = None,
                          event_id: str | None = None, status: str = "completed",
                          user_id: str | None = None) -> tuple[dict, str]:
    """(payload, event_id) shaped exactly like the AI worker emits it
    (services/ai/worker.py)."""
    return {
        "account_id": account_id,
        "user_id": user_id or _uid(),
        "job_id": job_id or _uid(),
        "model": "openai/gpt-4o-mini",
        "input_tokens": total_tokens // 5,
        "output_tokens": total_tokens - total_tokens // 5,
        "total_tokens": total_tokens,
        "cost_usd": 0.00012,
        "status": status,
        "usage_estimated": status == "cancelled",
    }, event_id or f"evt_{uuid.uuid4().hex}"


def _balance(client, account_id: str) -> int:
    r = client.get("/credits/balance", headers=_auth(account_id))
    assert r.status_code == 200, r.text
    return r.json()["balance"]


def _entries(db, account_id: str) -> list[LedgerEntry]:
    db.expire_all()
    return db.scalars(select(LedgerEntry).where(LedgerEntry.account_id == account_id)
                      .order_by(LedgerEntry.created_at, LedgerEntry.id)).all()


def _grant(client, account_id: str, plan: str = "pro") -> None:
    payload, event_id = _invoice_paid(account_id, plan=plan)
    consumer.handle_event("invoice.paid", payload, event_id)


# --------------------------------------------------------------------------
# Consumer: account.created -> free-tier starter grant (spec §4)
# --------------------------------------------------------------------------

def test_account_created_grants_starter_credits(client, db_session, published_events):
    """A brand-new account opens on the configured free-tier balance instead
    of 0 — one starter_grant row, distinct from a purchase."""
    account_id, owner_id = _uid(), _uid()
    payload, event_id = _account_created(account_id, owner_user_id=owner_id)
    consumer.handle_event("account.created", payload, event_id)

    assert _balance(client, account_id) == ledger.STARTER_GRANT

    entries = _entries(db_session, account_id)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.entry_type == "starter_grant"       # NOT purchase_grant
    assert entry.amount == ledger.STARTER_GRANT
    # No money changed hands, so nothing links this row to Stripe.
    assert entry.stripe_ref is None and entry.money_amount_cents is None
    assert entry.created_by_user_id == owner_id      # audit attribution (§6)
    assert "starter grant" in entry.reason
    # processed_events recorded the event_id (spec §7)
    assert db_session.get(ProcessedEvent, event_id) is not None

    assert [rk for rk, _ in published_events] == ["credits.credited"]
    emitted = published_events[0][1]
    assert emitted["entry_type"] == "starter_grant"
    assert emitted["amount"] == emitted["balance"] == ledger.STARTER_GRANT


def test_starter_grant_amount_is_configurable(client, db_session, monkeypatch):
    """CREDITS_STARTER_GRANT drives the amount; 0 switches the free tier off
    without writing a zero row (the ledger's 'never zero' invariant)."""
    monkeypatch.setattr(ledger, "STARTER_GRANT", 250)
    generous = _uid()
    consumer.handle_event("account.created", *_account_created(generous))
    assert _balance(client, generous) == 250

    monkeypatch.setattr(ledger, "STARTER_GRANT", 0)
    disabled = _uid()
    consumer.handle_event("account.created", *_account_created(disabled))
    assert _balance(client, disabled) == 0
    assert _entries(db_session, disabled) == []


def test_starter_grant_idempotent_on_event_redelivery(client, db_session, published_events):
    """Broker redelivery of the SAME event -> granted ONCE (processed_events)."""
    account_id = _uid()
    payload, event_id = _account_created(account_id)
    consumer.handle_event("account.created", payload, event_id)
    consumer.handle_event("account.created", payload, event_id)  # redelivery

    assert _balance(client, account_id) == ledger.STARTER_GRANT
    assert len(_entries(db_session, account_id)) == 1
    assert [rk for rk, _ in published_events] == ["credits.credited"]  # announced once


def test_starter_grant_idempotent_on_fresh_event_id_same_account(client, db_session):
    """The User service re-emits account.created under a NEW event_id (so
    processed_events cannot help) -> the account_id business-key guard still
    prevents a second welcome balance. Unlike the other handlers there is no
    invoice or job id to dedupe on: the account IS the business key."""
    account_id = _uid()
    payload, event_id = _account_created(account_id)
    consumer.handle_event("account.created", payload, event_id)
    consumer.handle_event("account.created", payload, f"evt_{uuid.uuid4().hex}")
    # ...and again with a differently-shaped payload for the same account.
    consumer.handle_event("account.created", *_account_created(account_id, type_="team"))

    assert _balance(client, account_id) == ledger.STARTER_GRANT
    assert len(_entries(db_session, account_id)) == 1


def test_account_created_without_account_id_writes_nothing(client, db_session):
    """Dropped rather than dead-lettered forever — no retry can supply the id
    we scope and dedupe by."""
    payload, event_id = _account_created(_uid())
    del payload["account_id"]
    consumer.handle_event("account.created", payload, event_id)
    assert db_session.scalars(select(LedgerEntry)).all() == []


def test_generation_debit_nets_against_the_starter_grant(client, db_session, published_events,
                                                         monkeypatch):
    """The Definition of Done's balance flow for a free account: signup grants
    credits, the first AI generation debits them, and the balance is the sum
    of the two rows — never negative on the very first generation, which is
    the whole point of the grant."""
    account_id = _uid()
    # Pinned rather than read from config so the arithmetic below is explicit
    # and does not move when the deployment retunes CREDITS_STARTER_GRANT.
    monkeypatch.setattr(ledger, "STARTER_GRANT", 500)

    consumer.handle_event("account.created", *_account_created(account_id))
    consumer.handle_event("ai.generation_completed",
                          *_generation_completed(account_id, total_tokens=2500))
    cost = ledger.credits_for_tokens(2500)  # ceil(2500/1000) = 3

    entries = _entries(db_session, account_id)
    assert [e.entry_type for e in entries] == ["starter_grant", "generation_debit"]
    assert [e.amount for e in entries] == [500, -cost]
    assert _balance(client, account_id) == 497 == sum(e.amount for e in entries)

    # The grant is what keeps this generation out of debt: no overdraft alert,
    # where without it the balance would have gone to -3 (see
    # test_insufficient_balance_records_the_debt_and_alerts).
    assert [p for rk, p in published_events if rk == "credits.low_balance"] == []


def test_starter_grant_survives_a_refund_clawback(client, db_session):
    """Free credits are not refundable value: refund.issued reverses
    purchase_grant rows only, so a refunded invoice can never eat the welcome
    balance. This is the concrete reason starter_grant is its own type."""
    account_id = _uid()
    consumer.handle_event("account.created", *_account_created(account_id))
    _grant(client, account_id, plan="pro")                       # +1000, refundable
    consumer.handle_event("refund.issued", *_refund_issued(account_id))

    entries = _entries(db_session, account_id)
    assert [e.entry_type for e in entries] == ["starter_grant", "purchase_grant", "refund_clawback"]
    assert _balance(client, account_id) == ledger.STARTER_GRANT


def test_starter_grant_reads_distinctly_in_the_history_view(client):
    """Transaction history must not present a gift as a purchase."""
    account_id = _uid()
    consumer.handle_event("account.created", *_account_created(account_id))
    _grant(client, account_id, plan="pro")

    entries = client.get("/credits/history", headers=_auth(account_id)).json()["entries"]
    by_type = {e["entry_type"]: e for e in entries}
    assert set(by_type) == {"starter_grant", "purchase_grant"}
    assert by_type["starter_grant"]["stripe_ref"] is None
    assert by_type["purchase_grant"]["stripe_ref"] is not None


# --------------------------------------------------------------------------
# Consumer: invoice.paid -> credit grant
# --------------------------------------------------------------------------

def test_invoice_paid_credits_account(client, db_session, published_events):
    account_id = _uid()
    payload, event_id = _invoice_paid(account_id, plan="pro")
    consumer.handle_event("invoice.paid", payload, event_id)

    assert _balance(client, account_id) == ledger.PLAN_CREDITS["pro"]
    entries = _entries(db_session, account_id)
    assert len(entries) == 1
    assert entries[0].entry_type == "purchase_grant"
    assert entries[0].stripe_ref == payload["stripe_invoice_id"]
    assert entries[0].money_amount_cents == 2900
    # processed_events recorded the event_id (spec §7)
    assert db_session.get(ProcessedEvent, event_id) is not None
    assert [rk for rk, _ in published_events] == ["credits.credited"]
    assert published_events[0][1]["amount"] == ledger.PLAN_CREDITS["pro"]


def test_consumer_idempotent_on_event_redelivery(client, db_session, published_events):
    """Same event delivered twice (broker redelivery) -> credited ONCE."""
    account_id = _uid()
    payload, event_id = _invoice_paid(account_id, plan="team")
    consumer.handle_event("invoice.paid", payload, event_id)
    consumer.handle_event("invoice.paid", payload, event_id)  # redelivery

    assert _balance(client, account_id) == ledger.PLAN_CREDITS["team"]
    assert len(_entries(db_session, account_id)) == 1
    assert [rk for rk, _ in published_events] == ["credits.credited"]  # announced once


def test_consumer_idempotent_on_fresh_event_id_same_invoice(client, db_session):
    """Producer re-emits the SAME invoice under a NEW event_id -> the
    business-key guard (stripe_invoice_id) still prevents double-crediting."""
    account_id = _uid()
    payload, event_id = _invoice_paid(account_id)
    consumer.handle_event("invoice.paid", payload, event_id)
    consumer.handle_event("invoice.paid", payload, f"evt_{uuid.uuid4().hex}")

    assert _balance(client, account_id) == ledger.PLAN_CREDITS["pro"]
    assert len(_entries(db_session, account_id)) == 1


def test_unknown_plan_grants_nothing(client, db_session):
    account_id = _uid()
    payload, event_id = _invoice_paid(account_id, plan="free")
    consumer.handle_event("invoice.paid", payload, event_id)
    assert _balance(client, account_id) == 0
    assert _entries(db_session, account_id) == []


# --------------------------------------------------------------------------
# Consumer: ai.generation_completed -> credit debit (spec §10)
# --------------------------------------------------------------------------

def test_generation_debits_the_ledger(client, db_session, published_events):
    account_id, user_id = _uid(), _uid()
    _grant(client, account_id, plan="pro")  # +1000
    published_events.clear()

    payload, event_id = _generation_completed(account_id, total_tokens=2500, user_id=user_id)
    consumer.handle_event("ai.generation_completed", payload, event_id)

    # ceil(2500 / 1000) * 1 = 3 credits
    cost = ledger.credits_for_tokens(2500)
    assert cost == 3
    assert _balance(client, account_id) == 1000 - cost

    entry = _entries(db_session, account_id)[-1]
    assert entry.entry_type == "generation_debit"
    assert entry.amount == -cost
    assert entry.job_id == payload["job_id"]          # the business key
    assert entry.created_by_user_id == user_id        # audit attribution (§6)
    assert "2500 tokens" in entry.reason
    assert db_session.get(ProcessedEvent, event_id) is not None

    assert [rk for rk, _ in published_events] == ["credits.debited"]
    emitted = published_events[0][1]
    assert emitted["amount"] == cost
    assert emitted["balance"] == 1000 - cost
    assert emitted["job_id"] == payload["job_id"]
    assert emitted["entry_type"] == "generation_debit"


def test_generation_debit_never_mutates_a_balance_in_place(client, db_session):
    """Spec §8 Service 5: the ledger is append-only. Three generations leave
    three rows and a balance that is exactly their sum."""
    account_id = _uid()
    _grant(client, account_id, plan="pro")  # +1000
    for tokens in (1000, 4200, 500):
        consumer.handle_event("ai.generation_completed", *_generation_completed(account_id, tokens))

    entries = _entries(db_session, account_id)
    assert [e.amount for e in entries] == [1000, -1, -5, -1]
    assert _balance(client, account_id) == sum(e.amount for e in entries) == 993


def test_generation_debit_idempotent_on_event_redelivery(client, db_session, published_events):
    """Broker redelivery of the SAME event -> debited ONCE (processed_events)."""
    account_id = _uid()
    _grant(client, account_id, plan="pro")
    payload, event_id = _generation_completed(account_id, total_tokens=3000)

    consumer.handle_event("ai.generation_completed", payload, event_id)
    consumer.handle_event("ai.generation_completed", payload, event_id)  # redelivery

    assert _balance(client, account_id) == 1000 - 3
    assert len([e for e in _entries(db_session, account_id)
                if e.entry_type == "generation_debit"]) == 1
    assert [rk for rk, _ in published_events].count("credits.debited") == 1


def test_generation_debit_idempotent_on_fresh_event_id_same_job(client, db_session):
    """The AI service re-emits the SAME job under a NEW event_id (so
    processed_events cannot help) -> the job_id business-key guard still
    prevents the double-debit."""
    account_id = _uid()
    _grant(client, account_id, plan="pro")
    payload, event_id = _generation_completed(account_id, total_tokens=3000)

    consumer.handle_event("ai.generation_completed", payload, event_id)
    consumer.handle_event("ai.generation_completed", payload, f"evt_{uuid.uuid4().hex}")

    assert _balance(client, account_id) == 1000 - 3
    assert len([e for e in _entries(db_session, account_id)
                if e.entry_type == "generation_debit"]) == 1


def test_insufficient_balance_records_the_debt_and_alerts(client, db_session, published_events):
    """The tokens were already streamed and already paid for upstream, so the
    debit lands even though it overdraws — hiding it would let the account
    keep generating for free. credits.low_balance carries `insufficient`."""
    account_id = _uid()
    consumer.handle_event("ai.generation_completed",
                          *_generation_completed(account_id, total_tokens=5000))

    assert _balance(client, account_id) == -5
    entry = _entries(db_session, account_id)[-1]
    assert entry.entry_type == "generation_debit" and entry.amount == -5

    low = [p for rk, p in published_events if rk == "credits.low_balance"]
    assert len(low) == 1
    assert low[0]["balance"] == -5
    assert low[0]["insufficient"] is True


def test_low_balance_alerts_on_the_threshold_crossing_too(client, published_events):
    account_id = _uid()
    _grant(client, account_id, plan="pro")           # 1000
    client.post("/credits/consume", json={"amount": 899}, headers=_auth(account_id))  # -> 101
    published_events.clear()

    # 101 -> 99 crosses LOW_BALANCE_THRESHOLD without going negative.
    consumer.handle_event("ai.generation_completed",
                          *_generation_completed(account_id, total_tokens=1500))
    low = [p for rk, p in published_events if rk == "credits.low_balance"]
    assert len(low) == 1
    assert low[0]["balance"] == 99 and low[0]["insufficient"] is False

    # Still below, but not newly so and not in debt -> no repeat alert.
    published_events.clear()
    consumer.handle_event("ai.generation_completed",
                          *_generation_completed(account_id, total_tokens=100))
    assert [p for rk, p in published_events if rk == "credits.low_balance"] == []


def test_cancelled_generation_is_still_charged(client, db_session):
    """The AI worker emits generation_completed with status='cancelled' and
    estimated counts because the provider billed the tokens it streamed."""
    account_id = _uid()
    _grant(client, account_id, plan="pro")
    consumer.handle_event("ai.generation_completed",
                          *_generation_completed(account_id, total_tokens=1200, status="cancelled"))
    assert _balance(client, account_id) == 1000 - ledger.credits_for_tokens(1200) == 998


def test_zero_token_and_malformed_generations_write_nothing(client, db_session):
    """A generation with nothing to meter is not a sale, and the ledger's
    'never zero' invariant holds. A payload missing the keys we scope and
    dedupe by is dropped rather than dead-lettered forever."""
    account_id = _uid()
    _grant(client, account_id, plan="pro")

    consumer.handle_event("ai.generation_completed",
                          *_generation_completed(account_id, total_tokens=0))
    payload, event_id = _generation_completed(account_id)
    del payload["job_id"]
    consumer.handle_event("ai.generation_completed", payload, event_id)

    assert _balance(client, account_id) == 1000
    assert [e.entry_type for e in _entries(db_session, account_id)] == ["purchase_grant"]


def test_generation_debit_appears_in_the_history_view(client):
    account_id = _uid()
    _grant(client, account_id, plan="pro")
    payload, event_id = _generation_completed(account_id, total_tokens=2000)
    consumer.handle_event("ai.generation_completed", payload, event_id)

    entries = client.get("/credits/history", headers=_auth(account_id)).json()["entries"]
    assert entries[0]["entry_type"] == "generation_debit"
    assert entries[0]["job_id"] == payload["job_id"]
    assert entries[0]["amount"] == -2


def test_job_id_column_is_added_to_a_pre_existing_ledger_table(tmp_path):
    """The failure mode the rest of this suite structurally cannot see.

    Every other test starts from an empty database, so create_all builds
    credits_ledger complete. A REAL deployment has the table already, and
    create_all never ALTERs one it can see — so job_id (and its index, which
    the double-debit guard looks up on every generation) would silently not
    exist, and every generation debit would fail. Rebuild the pre-job_id
    table shape and prove startup heals it.
    """
    from sqlalchemy import create_engine, inspect, text

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE credits_ledger (
                id VARCHAR(36) PRIMARY KEY,
                account_id VARCHAR(36) NOT NULL,
                amount INTEGER NOT NULL,
                entry_type VARCHAR(32) NOT NULL
            )
        """))
        conn.execute(text("INSERT INTO credits_ledger VALUES ('e1', 'a1', 1000, 'purchase_grant')"))

    added = database.add_missing_columns(
        eng, "credits_ledger", database.ADDED_COLUMNS["credits_ledger"],
        indexes=database.ADDED_INDEXES["credits_ledger"],
    )
    assert added == ["job_id"]

    inspector = inspect(eng)
    assert "job_id" in {c["name"] for c in inspector.get_columns("credits_ledger")}
    assert "ix_credits_ledger_job_id" in {i["name"] for i in inspector.get_indexes("credits_ledger")}

    # The pre-existing row survived untouched, with NULL for the new column —
    # it predates generation debits, which is exactly what NULL should mean.
    with eng.begin() as conn:
        assert conn.execute(text("SELECT amount, job_id FROM credits_ledger")).all() == [(1000, None)]

    # Idempotent: a restart adds nothing and does not fail on the index.
    assert database.add_missing_columns(
        eng, "credits_ledger", database.ADDED_COLUMNS["credits_ledger"],
        indexes=database.ADDED_INDEXES["credits_ledger"],
    ) == []


def test_credits_per_1k_tokens_formula():
    """ceil(total_tokens / 1000) credits, minimum 1 for any metered usage."""
    assert ledger.credits_for_tokens(0) == 0
    assert ledger.credits_for_tokens(1) == 1        # never free
    assert ledger.credits_for_tokens(1000) == 1
    assert ledger.credits_for_tokens(1001) == 2     # rounds up
    assert ledger.credits_for_tokens(10_000) == 10


# --------------------------------------------------------------------------
# Consumer: refund.issued -> claw-back
# --------------------------------------------------------------------------

def test_refund_claws_back_matching_grant(client, db_session, published_events):
    account_id = _uid()
    _grant(client, account_id, plan="pro")  # +1000, money 2900

    payload, event_id = _refund_issued(account_id, amount=2900)
    consumer.handle_event("refund.issued", payload, event_id)

    assert _balance(client, account_id) == 0
    entries = _entries(db_session, account_id)
    assert [e.entry_type for e in entries] == ["purchase_grant", "refund_clawback"]
    clawback = entries[1]
    assert clawback.amount == -entries[0].amount
    assert clawback.related_entry_id == entries[0].id  # points at the grant it reverses
    assert clawback.stripe_ref == payload["stripe_refund_id"]
    rks = [rk for rk, _ in published_events]
    assert "credits.debited" in rks
    assert "credits.low_balance" in rks  # 1000 -> 0 crosses the threshold

    # Redelivery of the same refund: no double claw-back.
    consumer.handle_event("refund.issued", payload, event_id)
    assert _balance(client, account_id) == 0
    assert len(_entries(db_session, account_id)) == 2


def test_second_refund_cannot_reverse_same_grant(client, db_session):
    """A DIFFERENT refund event (new event_id, new refund id) finds the only
    grant already reversed -> nothing more to claw back."""
    account_id = _uid()
    _grant(client, account_id)
    consumer.handle_event("refund.issued", *_refund_issued(account_id, amount=2900))
    consumer.handle_event("refund.issued", *_refund_issued(account_id, amount=2900))

    assert _balance(client, account_id) == 0  # not -1000
    assert len(_entries(db_session, account_id)) == 2


def test_refund_without_grant_is_noop(client, db_session):
    account_id = _uid()
    consumer.handle_event("refund.issued", *_refund_issued(account_id))
    assert _balance(client, account_id) == 0
    assert _entries(db_session, account_id) == []


# --------------------------------------------------------------------------
# Balance + history are derived from the ledger; consumption
# --------------------------------------------------------------------------

def test_balance_is_sum_of_ledger_rows(client, db_session):
    account_id = _uid()
    _grant(client, account_id, plan="pro")    # +1000
    r = client.post("/credits/consume", json={"amount": 300, "reason": "ai generation"},
                    headers=_auth(account_id, role="member"))
    assert r.status_code == 201, r.text
    _grant(client, account_id, plan="team")   # +5000

    entries = _entries(db_session, account_id)
    assert [e.amount for e in entries] == [1000, -300, 5000]
    assert _balance(client, account_id) == sum(e.amount for e in entries) == 5700

    r = client.get("/credits/history", headers=_auth(account_id, role="member"))
    body = r.json()
    assert body["balance"] == 5700
    assert [e["amount"] for e in body["entries"]] == [5000, -300, 1000]  # newest first
    assert body["entries"][1]["entry_type"] == "usage_debit"


def test_over_balance_debit_rejected(client, db_session, published_events):
    account_id = _uid()
    _grant(client, account_id, plan="pro")  # 1000
    r = client.post("/credits/consume", json={"amount": 1001}, headers=_auth(account_id))
    assert r.status_code == 409
    assert _balance(client, account_id) == 1000
    assert len(_entries(db_session, account_id)) == 1  # no debit row written
    assert [rk for rk, _ in published_events] == ["credits.credited"]  # only the grant


def test_low_balance_emitted_once_on_crossing(client, published_events):
    account_id = _uid()
    _grant(client, account_id, plan="pro")  # 1000
    client.post("/credits/consume", json={"amount": 950}, headers=_auth(account_id))  # -> 50: crosses
    client.post("/credits/consume", json={"amount": 10}, headers=_auth(account_id))   # -> 40: already below

    low = [p for rk, p in published_events if rk == "credits.low_balance"]
    assert len(low) == 1
    assert low[0] == {"account_id": account_id, "balance": 50, "threshold": ledger.LOW_BALANCE_THRESHOLD}


def test_endpoints_require_valid_token(client):
    assert client.get("/credits/balance").status_code == 401
    assert client.get("/credits/history", headers={"Authorization": "Bearer garbage"}).status_code == 401


# --------------------------------------------------------------------------
# Marketplace: list -> purchase transfers atomically
# --------------------------------------------------------------------------

def _list_credits(client, seller: str, amount: int = 200, price: int = 500) -> str:
    r = client.post("/credits/marketplace/listings",
                    json={"credits_amount": amount, "price_cents": price},
                    headers=_auth(seller))
    assert r.status_code == 201, r.text
    return r.json()["listing_id"]


def test_marketplace_purchase_transfers_credits_atomically(client, db_session, published_events):
    seller, buyer = _uid(), _uid()
    _grant(client, seller, plan="pro")  # 1000
    listing_id = _list_credits(client, seller, amount=200, price=500)

    r = client.post(f"/credits/marketplace/listings/{listing_id}/purchase", headers=_auth(buyer))
    assert r.status_code == 201, r.text
    assert r.json()["buyer_balance"] == 200

    # Both sides of the transfer landed, as one +/- pair tied to the listing.
    assert _balance(client, seller) == 800
    assert _balance(client, buyer) == 200
    seller_rows = [e for e in _entries(db_session, seller) if e.listing_id == listing_id]
    buyer_rows = [e for e in _entries(db_session, buyer) if e.listing_id == listing_id]
    assert [e.amount for e in seller_rows] == [-200]
    assert [e.amount for e in buyer_rows] == [200]
    assert seller_rows[0].counterparty_account_id == buyer
    assert buyer_rows[0].counterparty_account_id == seller

    listing = db_session.get(MarketplaceListing, listing_id)
    assert listing.status == "sold"
    assert listing.buyer_account_id == buyer
    assert listing.sold_at is not None

    rks = [rk for rk, _ in published_events]
    assert rks.count("credits.debited") == 1   # seller side
    assert rks.count("credits.credited") == 2  # grant + buyer side


def test_listing_rejected_over_balance(client, db_session):
    seller = _uid()
    _grant(client, seller, plan="pro")  # 1000
    r = client.post("/credits/marketplace/listings",
                    json={"credits_amount": 1001, "price_cents": 500}, headers=_auth(seller))
    assert r.status_code == 409
    assert db_session.scalars(select(MarketplaceListing)).all() == []


def test_purchase_rejected_when_seller_spent_the_credits(client, db_session):
    """No escrow at list time -> the purchase transaction re-checks the
    seller's balance; failure leaves NO ledger rows and the listing open
    (the atomicity guarantee, exercised via the rollback path)."""
    seller, buyer = _uid(), _uid()
    _grant(client, seller, plan="pro")  # 1000
    listing_id = _list_credits(client, seller, amount=200)
    client.post("/credits/consume", json={"amount": 900}, headers=_auth(seller))  # balance 100 < 200

    r = client.post(f"/credits/marketplace/listings/{listing_id}/purchase", headers=_auth(buyer))
    assert r.status_code == 409
    assert _balance(client, buyer) == 0                      # buyer NOT credited
    assert _balance(client, seller) == 100                   # seller NOT debited
    assert not [e for e in _entries(db_session, seller) if e.listing_id == listing_id]
    assert db_session.get(MarketplaceListing, listing_id).status == "open"  # claim rolled back


def test_sold_listing_cannot_be_purchased_again(client, db_session):
    seller, buyer1, buyer2 = _uid(), _uid(), _uid()
    _grant(client, seller, plan="pro")
    listing_id = _list_credits(client, seller, amount=200)
    assert client.post(f"/credits/marketplace/listings/{listing_id}/purchase",
                       headers=_auth(buyer1)).status_code == 201
    assert client.post(f"/credits/marketplace/listings/{listing_id}/purchase",
                       headers=_auth(buyer2)).status_code == 409
    assert _balance(client, buyer2) == 0
    assert _balance(client, seller) == 800  # debited exactly once


def test_cannot_buy_own_listing_and_marketplace_is_owner_only(client):
    seller = _uid()
    _grant(client, seller, plan="pro")
    listing_id = _list_credits(client, seller, amount=100)
    # own listing
    assert client.post(f"/credits/marketplace/listings/{listing_id}/purchase",
                       headers=_auth(seller)).status_code == 409
    # member role may browse but not sell/buy
    member = _auth(_uid(), role="member")
    assert client.get("/credits/marketplace/listings", headers=member).status_code == 200
    assert client.post("/credits/marketplace/listings",
                       json={"credits_amount": 10, "price_cents": 10}, headers=member).status_code == 403
    assert client.post(f"/credits/marketplace/listings/{listing_id}/purchase",
                       headers=member).status_code == 403


def test_canceled_listing_disappears_and_cannot_sell(client):
    seller, buyer = _uid(), _uid()
    _grant(client, seller, plan="pro")
    listing_id = _list_credits(client, seller, amount=100)
    r = client.delete(f"/credits/marketplace/listings/{listing_id}", headers=_auth(seller))
    assert r.status_code == 200 and r.json()["status"] == "canceled"
    assert client.get("/credits/marketplace/listings",
                      headers=_auth(buyer)).json()["listings"] == []
    assert client.post(f"/credits/marketplace/listings/{listing_id}/purchase",
                       headers=_auth(buyer)).status_code == 409
