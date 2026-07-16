"""
Admin service tests: the admin-role gate holds on EVERY route (a valid
member token answers 403 everywhere, missing/garbage/refresh tokens answer
401), the `#`-bound consumer turns every domain event into exactly one
append-only audit_log row (idempotent against redelivery, actor/account
extracted, payload preserved), the audit read routes filter/paginate and
never leak across tenants, the session viewer reads the exact Redis keys
Auth writes and revocation truly deletes the jti (the platform's
invalidation switch), suspend/reactivate flips the directory row AND
force-logs-out the target's live sessions (SuperAdmin only), and the
aggregate overview forwards the caller's bearer to the mocked
User/Credits/Usage clients and degrades per-section instead of failing.

No infra: SQLite via conftest, fakeredis for the session store, every
clients.py function faked (base URLs also point at a dead address so nothing
can reach the network — proven by a test that calls the REAL client
function), and consumer.handle_event called directly (the exact function the
broker would). Nothing to stub for publishing: this service publishes none.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from creditflow_common import jwt_utils
from creditflow_common.idempotency import ProcessedEvent

import clients
import consumer
import main
import sessions
from conftest import REAL_GET_CREDIT_BALANCE
from models import AccountDirectory, AuditLog, UserDirectory


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(role: str, account_id: str | None = None, user_id: str | None = None) -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues."""
    token, _ = jwt_utils.sign_access_token(user_id or _uid(), account_id or _uid(), role)
    return {"Authorization": f"Bearer {token}"}


def _super() -> dict:
    return _auth("superadmin")


def _handle(routing_key: str, data: dict, event_id: str | None = None,
            exchange: str | None = None) -> str:
    """Drive the consumer exactly like the broker would; returns the event_id."""
    event_id = event_id or _uid()
    consumer.handle_event(routing_key, data, event_id, exchange=exchange)
    return event_id


def _seed_session(redis, account_id: str, user_id: str | None = None,
                  role: str = "owner", jti: str | None = None, ttl: int = 900) -> str:
    """Write a session key EXACTLY the way services/auth/store.py does."""
    jti = jti or _uid()
    redis.set(
        "session:" + jti,
        json.dumps({"user_id": user_id or _uid(), "account_id": account_id,
                    "role": role, "issued_at": int(time.time())}),
        ex=ttl,
    )
    return jti


def _seed_account(account_id: str | None = None, name: str = "Acme Corp") -> str:
    """Seed the directory through the consumer — the only writer there is."""
    account_id = account_id or _uid()
    _handle("account.created", {"account_id": account_id, "type": "team", "name": name,
                                "plan_tier": "free", "owner_user_id": _uid()},
            exchange="account_events")
    return account_id


def _seed_user(user_id: str | None = None, email: str = "person@example.com") -> str:
    user_id = user_id or _uid()
    _handle("user.registered", {"user_id": user_id, "email": email,
                                "verification_token": "vtok"},
            exchange="user_events")
    return user_id


# --------------------------------------------------------------------------
# Queue contract — every domain exchange, `#`-bound, `admin.<exchange>` names
# --------------------------------------------------------------------------

def test_bindings_cover_every_exchange_with_wildcard():
    """The spec binds `#` on ALL exchanges; the queue names must follow the
    `admin.<exchange>` convention — Notification pre-declared
    admin.notification_events for us, so any other name would strand its
    accumulating backlog."""
    assert {(b.exchange, b.queue, b.routing_keys) for b in consumer.BINDINGS} == {
        (exchange, f"admin.{exchange}", ("#",))
        for exchange in (
            "user_events", "account_events", "billing_events", "credits_events",
            "usage_events", "content_events", "scheduler_events", "social_events",
            "scraper_events", "notification_events",
        )
    }


# --------------------------------------------------------------------------
# The admin-role gate — the most important property of this service
# --------------------------------------------------------------------------

def _all_admin_routes() -> list[tuple[str, str]]:
    """(method, concrete-path) for every registered route except /health,
    with path params filled in — enumerated from the app itself so a newly
    added route can never dodge the gate tests."""
    fill = {"account_id": _uid(), "user_id": _uid(), "jti": _uid(), "audit_id": _uid()}
    out = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute) or route.path == "/health":
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, route.path.format(**fill)))
    return out


def test_route_enumeration_found_the_full_surface():
    # 14 admin routes today — if this number changes, the gate tests below
    # automatically cover the new route; this assertion just proves the
    # enumeration isn't silently empty or missing registrations.
    assert len(_all_admin_routes()) == 14


def test_member_token_gets_403_on_every_route(client):
    """A VALID token with the non-admin `member` role must be refused
    everywhere — this service has no non-admin surface."""
    headers = _auth("member")
    for method, path in _all_admin_routes():
        resp = client.request(method, path, headers=headers)
        assert resp.status_code == 403, f"{method} {path} answered {resp.status_code}"
        assert resp.json()["detail"] == "Requires an admin role"


def test_unknown_role_gets_403_on_every_route(client):
    headers = _auth("viewer")  # not member either — anything non-admin is out
    for method, path in _all_admin_routes():
        assert client.request(method, path, headers=headers).status_code == 403


def test_missing_token_gets_401_on_every_route(client):
    for method, path in _all_admin_routes():
        assert client.request(method, path).status_code == 401


def test_garbage_token_gets_401(client):
    resp = client.get("/admin/audit-log", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_refresh_token_is_not_an_access_token(client):
    token, _ = jwt_utils.sign_refresh_token(_uid(), _uid())
    resp = client.get("/admin/audit-log", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not an access token"


def test_tenant_admin_roles_pass_the_gate(client):
    for role in ("owner", "admin"):
        assert client.get("/admin/audit-log", headers=_auth(role)).status_code == 200
    assert client.get("/admin/audit-log", headers=_super()).status_code == 200


SUPERADMIN_ONLY = (
    ("GET", "/admin/users"),
    ("GET", "/admin/stats"),
)


def test_superadmin_only_routes_refuse_tenant_admins(client):
    for method, path in SUPERADMIN_ONLY:
        resp = client.request(method, path, headers=_auth("owner"))
        assert resp.status_code == 403, f"{method} {path}"
        assert resp.json()["detail"] == "Requires the superadmin role"


# --------------------------------------------------------------------------
# Audit-log ingestion (consumer) + idempotent redelivery
# --------------------------------------------------------------------------

def test_event_lands_as_one_audit_row_with_payload_preserved(client, db_session):
    account_id = _uid()
    event_id = _handle("invoice.paid", {"account_id": account_id, "plan": "pro",
                                        "amount_cents": 2900},
                       exchange="billing_events")

    (row,) = db_session.scalars(select(AuditLog)).all()
    assert row.event_id == event_id
    assert row.exchange == "billing_events"
    assert row.routing_key == "invoice.paid"
    assert row.account_id == account_id
    assert json.loads(row.payload) == {"account_id": account_id, "plan": "pro",
                                       "amount_cents": 2900}
    assert db_session.get(ProcessedEvent, event_id) is not None


def test_redelivered_event_never_appends_twice(client, db_session):
    event_id = _uid()
    payload = {"account_id": _uid(), "plan": "team"}
    _handle("invoice.paid", payload, event_id=event_id, exchange="billing_events")
    _handle("invoice.paid", payload, event_id=event_id, exchange="billing_events")

    assert len(db_session.scalars(select(AuditLog)).all()) == 1
    assert len(db_session.scalars(select(ProcessedEvent)).all()) == 1


def test_actor_extracted_from_by_user_id_fields(client, db_session):
    actor = _uid()
    _handle("account.updated", {"account_id": _uid(), "change": "member_removed",
                                "user_id": _uid(), "updated_by_user_id": actor},
            exchange="account_events")
    (row,) = db_session.scalars(select(AuditLog)).all()
    # The *_by_user_id field wins over the subject's user_id.
    assert row.actor_user_id == actor


def test_user_event_scoped_by_user_id_placeholder_convention(client, db_session):
    """user_events carry no account_id — user_id scopes the row, matching
    today's placeholder tokens (account_id == user_id)."""
    user_id = _uid()
    _handle("user.logged_in", {"user_id": user_id, "email": "x@y.z", "jti": _uid()},
            exchange="user_events")
    (row,) = db_session.scalars(select(AuditLog)).all()
    assert row.account_id == user_id
    assert row.actor_user_id == user_id


def test_event_without_account_still_audited(client, db_session):
    _handle("notification.sent", {}, exchange="notification_events")
    (row,) = db_session.scalars(select(AuditLog)).all()
    assert row.account_id is None
    assert row.actor_user_id is None


# --------------------------------------------------------------------------
# Audit-log read routes: filters, pagination, tenant isolation
# --------------------------------------------------------------------------

def test_audit_list_filters(client):
    acc_a, acc_b = _uid(), _uid()
    actor = _uid()
    _handle("invoice.paid", {"account_id": acc_a, "plan": "pro"}, exchange="billing_events")
    _handle("credits.debited", {"account_id": acc_a, "amount": 5}, exchange="credits_events")
    _handle("account.updated", {"account_id": acc_b, "change": "profile", "name": "B",
                                "updated_by_user_id": actor}, exchange="account_events")

    def _items(**params):
        resp = client.get("/admin/audit-log", params=params, headers=_super())
        assert resp.status_code == 200
        return resp.json()

    assert _items()["total"] == 3
    assert _items(account_id=acc_a)["total"] == 2
    body = _items(routing_key="invoice.paid")
    assert body["total"] == 1 and body["items"][0]["account_id"] == acc_a
    assert _items(exchange="credits_events")["total"] == 1
    body = _items(actor_user_id=actor)
    assert body["total"] == 1 and body["items"][0]["routing_key"] == "account.updated"
    assert _items(account_id=acc_a, routing_key="credits.debited")["total"] == 1


def test_audit_list_time_range(client):
    acc = _uid()
    _handle("invoice.paid", {"account_id": acc, "plan": "pro"}, exchange="billing_events")
    cutoff = datetime.now(timezone.utc).isoformat()
    time.sleep(0.01)
    _handle("credits.credited", {"account_id": acc, "amount": 100}, exchange="credits_events")

    resp = client.get("/admin/audit-log", params={"since": cutoff}, headers=_super())
    assert [i["routing_key"] for i in resp.json()["items"]] == ["credits.credited"]
    resp = client.get("/admin/audit-log", params={"until": cutoff}, headers=_super())
    assert [i["routing_key"] for i in resp.json()["items"]] == ["invoice.paid"]

    resp = client.get("/admin/audit-log", params={"since": "yesterday"}, headers=_super())
    assert resp.status_code == 422


def test_audit_list_pagination(client):
    acc = _uid()
    ids = {_handle("credits.debited", {"account_id": acc, "n": i}, exchange="credits_events")
           for i in range(5)}

    page1 = client.get("/admin/audit-log", params={"limit": 3}, headers=_super()).json()
    page2 = client.get("/admin/audit-log", params={"limit": 3, "offset": 3},
                       headers=_super()).json()
    assert (page1["total"], page2["total"]) == (5, 5)
    assert len(page1["items"]) == 3 and len(page2["items"]) == 2
    assert {i["event_id"] for i in page1["items"] + page2["items"]} == ids


def test_audit_list_tenant_admin_is_force_scoped(client):
    acc_mine, acc_other = _uid(), _uid()
    _handle("invoice.paid", {"account_id": acc_mine, "plan": "pro"}, exchange="billing_events")
    _handle("invoice.paid", {"account_id": acc_other, "plan": "team"}, exchange="billing_events")

    body = client.get("/admin/audit-log", headers=_auth("owner", account_id=acc_mine)).json()
    assert body["total"] == 1
    assert body["items"][0]["account_id"] == acc_mine

    # Explicitly asking for a foreign account is refused outright.
    resp = client.get("/admin/audit-log", params={"account_id": acc_other},
                      headers=_auth("owner", account_id=acc_mine))
    assert resp.status_code == 403


def test_audit_entry_get_and_tenant_404(client):
    acc_mine, acc_other = _uid(), _uid()
    _handle("invoice.paid", {"account_id": acc_other, "plan": "pro"},
            exchange="billing_events")
    (entry,) = client.get("/admin/audit-log", headers=_super()).json()["items"]

    resp = client.get(f"/admin/audit-log/{entry['audit_id']}", headers=_super())
    assert resp.status_code == 200 and resp.json()["account_id"] == acc_other

    # Cross-tenant read answers 404, never 403 — ids don't leak existence.
    resp = client.get(f"/admin/audit-log/{entry['audit_id']}",
                      headers=_auth("owner", account_id=acc_mine))
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Directory read model (accounts + users, learned from consumed events)
# --------------------------------------------------------------------------

def test_account_created_populates_directory(client, db_session):
    owner = _uid()
    acc = _uid()
    _handle("account.created", {"account_id": acc, "type": "team", "name": "Acme",
                                "plan_tier": "free", "owner_user_id": owner},
            exchange="account_events")
    row = db_session.get(AccountDirectory, acc)
    assert (row.name, row.type, row.plan_tier, row.owner_user_id, row.status) == \
        ("Acme", "team", "free", owner, "active")


def test_directory_learns_renames_plan_changes_and_members(client, db_session):
    acc = _seed_account(name="Before")
    _handle("account.updated", {"account_id": acc, "change": "profile", "name": "After",
                                "updated_by_user_id": _uid()}, exchange="account_events")
    _handle("invoice.paid", {"account_id": acc, "plan": "pro"}, exchange="billing_events")
    member = _uid()
    _handle("member.joined", {"account_id": acc, "account_name": "After", "user_id": member,
                              "email": "m@x.y", "role": "member"}, exchange="account_events")

    db_session.expire_all()
    row = db_session.get(AccountDirectory, acc)
    assert (row.name, row.plan_tier) == ("After", "pro")
    user = db_session.get(UserDirectory, member)
    assert user.email == "m@x.y"


def test_later_events_never_clobber_suspension(client, db_session):
    acc = _seed_account()
    assert client.post(f"/admin/accounts/{acc}/suspend", headers=_super()).status_code == 200
    _handle("invoice.paid", {"account_id": acc, "plan": "team"}, exchange="billing_events")

    db_session.expire_all()
    row = db_session.get(AccountDirectory, acc)
    assert row.status == "suspended"
    assert row.plan_tier == "team"  # identity fields still learn


# --------------------------------------------------------------------------
# Account directory routes + suspend/reactivate
# --------------------------------------------------------------------------

def test_accounts_list_search_and_status_filter(client):
    acc_a = _seed_account(name="Acme Corp")
    _seed_account(name="Globex")
    client.post(f"/admin/accounts/{acc_a}/suspend", headers=_super())

    body = client.get("/admin/accounts", headers=_super()).json()
    assert body["total"] == 2
    body = client.get("/admin/accounts", params={"q": "acme"}, headers=_super()).json()
    assert body["total"] == 1 and body["items"][0]["account_id"] == acc_a
    body = client.get("/admin/accounts", params={"status": "suspended"}, headers=_super()).json()
    assert [i["account_id"] for i in body["items"]] == [acc_a]
    assert client.get("/admin/accounts", params={"status": "nope"},
                      headers=_super()).status_code == 422


def test_accounts_list_tenant_admin_sees_only_their_own_row(client):
    acc_mine = _seed_account(name="Mine")
    _seed_account(name="Other")
    body = client.get("/admin/accounts", headers=_auth("owner", account_id=acc_mine)).json()
    assert body["total"] == 1
    assert body["items"][0]["account_id"] == acc_mine


def test_account_get_cross_tenant_is_404(client):
    acc_other = _seed_account()
    resp = client.get(f"/admin/accounts/{acc_other}", headers=_auth("owner"))
    assert resp.status_code == 404
    assert client.get(f"/admin/accounts/{_uid()}", headers=_super()).status_code == 404


def test_suspend_account_flips_row_and_revokes_its_sessions(client, fake_redis, db_session):
    acc, other_acc = _seed_account(), _seed_account()
    jti_a1 = _seed_session(fake_redis, acc)
    jti_a2 = _seed_session(fake_redis, acc)
    jti_other = _seed_session(fake_redis, other_acc)

    resp = client.post(f"/admin/accounts/{acc}/suspend",
                       json={"reason": "fraud review"}, headers=_super())
    assert resp.status_code == 200
    assert resp.json() == {"account_id": acc, "status": "suspended", "sessions_revoked": 2}

    # The switch is REAL: the jtis are gone from Redis, so the Gateway
    # (which treats a token as valid only while its key exists) refuses them.
    assert fake_redis.exists("session:" + jti_a1) == 0
    assert fake_redis.exists("session:" + jti_a2) == 0
    assert fake_redis.exists("session:" + jti_other) == 1

    row = db_session.get(AccountDirectory, acc)
    assert (row.status, row.suspend_reason) == ("suspended", "fraud review")
    assert row.suspended_at is not None and row.suspended_by is not None


def test_reactivate_account_clears_suspension(client, db_session):
    acc = _seed_account()
    client.post(f"/admin/accounts/{acc}/suspend", json={"reason": "x"}, headers=_super())
    resp = client.post(f"/admin/accounts/{acc}/reactivate", headers=_super())
    assert resp.status_code == 200

    row = db_session.get(AccountDirectory, acc)
    assert (row.status, row.suspended_at, row.suspended_by, row.suspend_reason) == \
        ("active", None, None, None)


def test_suspend_account_is_superadmin_only_and_404_on_unknown(client):
    acc = _seed_account()
    for path in (f"/admin/accounts/{acc}/suspend", f"/admin/accounts/{acc}/reactivate"):
        resp = client.post(path, headers=_auth("owner", account_id=acc))
        assert resp.status_code == 403  # even the account's own owner may not
    assert client.post(f"/admin/accounts/{_uid()}/suspend",
                       headers=_super()).status_code == 404


# --------------------------------------------------------------------------
# User directory routes + suspend/reactivate
# --------------------------------------------------------------------------

def test_users_list_and_search_superadmin_only(client):
    u1 = _seed_user(email="alice@example.com")
    _seed_user(email="bob@example.com")

    body = client.get("/admin/users", headers=_super()).json()
    assert body["total"] == 2
    body = client.get("/admin/users", params={"q": "alice"}, headers=_super()).json()
    assert body["total"] == 1 and body["items"][0]["user_id"] == u1

    assert client.get("/admin/users", headers=_auth("owner")).status_code == 403
    assert client.get(f"/admin/users/{u1}", headers=_auth("admin")).status_code == 403
    assert client.get(f"/admin/users/{_uid()}", headers=_super()).status_code == 404


def test_suspend_user_revokes_their_sessions_across_accounts(client, fake_redis, db_session):
    user_id = _seed_user()
    jti_1 = _seed_session(fake_redis, account_id=_uid(), user_id=user_id)
    jti_2 = _seed_session(fake_redis, account_id=_uid(), user_id=user_id)
    jti_other = _seed_session(fake_redis, account_id=_uid(), user_id=_uid())

    resp = client.post(f"/admin/users/{user_id}/suspend",
                       json={"reason": "abuse"}, headers=_super())
    assert resp.status_code == 200
    assert resp.json()["sessions_revoked"] == 2
    assert fake_redis.exists("session:" + jti_1) == 0
    assert fake_redis.exists("session:" + jti_2) == 0
    assert fake_redis.exists("session:" + jti_other) == 1

    assert db_session.get(UserDirectory, user_id).status == "suspended"

    resp = client.post(f"/admin/users/{user_id}/reactivate", headers=_super())
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(UserDirectory, user_id).status == "active"


def test_suspend_user_is_superadmin_only(client):
    user_id = _seed_user()
    resp = client.post(f"/admin/users/{user_id}/suspend", headers=_auth("owner"))
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Active sessions: list + revoke (Redis via fakeredis)
# --------------------------------------------------------------------------

def test_sessions_list_all_and_filtered(client, fake_redis):
    acc_a, acc_b = _uid(), _uid()
    user = _uid()
    _seed_session(fake_redis, acc_a, user_id=user)
    _seed_session(fake_redis, acc_a)
    _seed_session(fake_redis, acc_b)

    body = client.get("/admin/sessions", headers=_super()).json()
    assert body["total"] == 3
    session = next(s for s in body["items"] if s["user_id"] == user)
    assert session["account_id"] == acc_a
    assert session["expires_in_seconds"] is not None and session["expires_in_seconds"] > 0

    body = client.get("/admin/sessions", params={"account_id": acc_a}, headers=_super()).json()
    assert body["total"] == 2
    body = client.get("/admin/sessions", params={"user_id": user}, headers=_super()).json()
    assert body["total"] == 1


def test_sessions_list_tenant_admin_scoped_to_own_account(client, fake_redis):
    acc_mine, acc_other = _uid(), _uid()
    _seed_session(fake_redis, acc_mine)
    _seed_session(fake_redis, acc_other)

    body = client.get("/admin/sessions", headers=_auth("owner", account_id=acc_mine)).json()
    assert body["total"] == 1
    assert body["items"][0]["account_id"] == acc_mine

    resp = client.get("/admin/sessions", params={"account_id": acc_other},
                      headers=_auth("owner", account_id=acc_mine))
    assert resp.status_code == 403


def test_revoke_session_truly_invalidates(client, fake_redis):
    acc = _uid()
    jti = _seed_session(fake_redis, acc)

    resp = client.delete(f"/admin/sessions/{jti}", headers=_super())
    assert resp.status_code == 200
    assert resp.json() == {"jti": jti, "revoked": True}

    # Invalidated for real: the key is gone, so the Gateway/Auth (which check
    # existence) refuse the token, and this service can't see it either.
    assert fake_redis.exists("session:" + jti) == 0
    assert sessions.get_session(jti) is None
    # Revoking again: it no longer exists.
    assert client.delete(f"/admin/sessions/{jti}", headers=_super()).status_code == 404


def test_revoke_session_cross_tenant_is_404_and_leaves_it_live(client, fake_redis):
    acc_other = _uid()
    jti = _seed_session(fake_redis, acc_other)

    resp = client.delete(f"/admin/sessions/{jti}", headers=_auth("owner"))
    assert resp.status_code == 404
    assert fake_redis.exists("session:" + jti) == 1  # untouched

    # The account's own tenant admin may revoke it.
    resp = client.delete(f"/admin/sessions/{jti}",
                         headers=_auth("admin", account_id=acc_other))
    assert resp.status_code == 200
    assert fake_redis.exists("session:" + jti) == 0


# --------------------------------------------------------------------------
# Aggregate per-account overview (mocked read-only clients)
# --------------------------------------------------------------------------

def test_overview_aggregates_and_forwards_the_callers_bearer(client, fake_redis, downstream):
    acc = _seed_account(name="Acme")
    _seed_session(fake_redis, acc)
    _seed_session(fake_redis, acc)
    headers = _auth("owner", account_id=acc)

    resp = client.get(f"/admin/accounts/{acc}/overview", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["directory"]["name"] == "Acme"
    assert body["active_sessions"] == 2
    assert body["profile"]["account_id"] == acc
    assert len(body["members"]["members"]) == 2
    assert body["credits"] == {"balance": 750}
    assert body["usage"] == {"tokens_used": 12000, "cost_usd": 1.44}
    assert body["degraded"] == []

    # Read-only calls carry the CALLER's bearer, never a minted one.
    expected_token = headers["Authorization"].split(" ", 1)[1]
    assert downstream["account_calls"][0]["bearer_token"] == expected_token
    assert downstream["balance_calls"][0]["bearer_token"] == expected_token


def test_overview_degrades_per_section_on_downstream_failure(client, downstream):
    acc = _seed_account()
    downstream["errors"]["get_credit_balance"] = clients.ClientError("credits down")
    downstream["errors"]["get_usage_summary"] = clients.ClientError("HTTP 503",
                                                                    status_code=503)

    resp = client.get(f"/admin/accounts/{acc}/overview",
                      headers=_auth("owner", account_id=acc))
    assert resp.status_code == 200
    body = resp.json()
    assert body["credits"] is None and body["usage"] is None
    assert sorted(body["degraded"]) == ["credits", "usage"]
    assert body["profile"] is not None  # healthy sections unaffected


def test_overview_cross_tenant_is_404(client):
    acc_other = _seed_account()
    resp = client.get(f"/admin/accounts/{acc_other}/overview", headers=_auth("owner"))
    assert resp.status_code == 404


def test_overview_superadmin_on_undirectoried_account_still_answers(client):
    """SuperAdmin may inspect an account the read model hasn't seen yet —
    the directory section is just None."""
    acc = _uid()
    resp = client.get(f"/admin/accounts/{acc}/overview", headers=_super())
    assert resp.status_code == 200
    assert resp.json()["directory"] is None


# --------------------------------------------------------------------------
# Platform stats
# --------------------------------------------------------------------------

def test_stats_counts_the_platform(client, fake_redis):
    acc = _seed_account()
    _seed_account()
    _seed_user()
    _seed_session(fake_redis, acc)
    client.post(f"/admin/accounts/{acc}/suspend", headers=_super())

    body = client.get("/admin/stats", headers=_super()).json()
    assert body["accounts"] == {"total": 2, "suspended": 1}
    assert body["users"]["total"] == 1
    # account.created x2 + user.registered = 3 audited events
    assert body["audit_events"] == 3
    assert body["active_sessions"] == 0  # suspension revoked the one session


# --------------------------------------------------------------------------
# Dead-address guard — an unmocked downstream call must fail instantly
# --------------------------------------------------------------------------

def test_unmocked_client_call_fails_fast_without_network(client):
    """conftest points USER/CREDITS/USAGE_URL at 127.0.0.1:9 (nothing
    listens); the REAL client function must raise ClientError immediately —
    proof no test can accidentally reach a live service."""
    with pytest.raises(clients.ClientError):
        REAL_GET_CREDIT_BALANCE("some-token")
