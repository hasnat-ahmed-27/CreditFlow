"""
Gateway suite — runs with plain pytest, no infra, no network (see conftest.py:
every upstream is a MockTransport, Redis is fakeredis, the RabbitMQ publisher
is captured, and real URLs point at a dead address).

Covers:
  - prefix -> upstream routing for every public route family
  - method/path/query/body preservation and verbatim Authorization passthrough
  - faithful upstream status/body/headers and the gateway error schema
  - CORS, X-Request-ID
  - JWT verification: valid / missing / invalid-signature / expired / wrong-type
  - public routes bypassing auth
  - role enforcement (owner-only, manager-only, admin-console) -> 403
  - Redis rate limiting: per-IP and per-account
  - webhook signature verification (valid + tampered), Redis SETNX dedup,
    normalized event publishing, and the Stripe relay to Billing
  - the response-aggregation endpoint
  - and — the load-bearing one — SSE passthrough: chunks arrive in order AND
    the upstream is consumed only as fast as the client reads (no buffering).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

import main
import proxy
import ratelimit
import signatures
import webhooks
from conftest import (
    LINKEDIN_TEST_SECRET,
    OPENROUTER_TEST_SECRET,
    STRIPE_TEST_SECRET,
    WRONG_PRIVATE_KEY,
    bearer,
    make_token,
)

# ---------------------------------------------------------------------------
# Routing: every public prefix -> the owning service (compose topology).
# An OWNER token clears both authentication and every role gate these GETs hit.
# ---------------------------------------------------------------------------

ROUTES = [
    ("/auth/login", "http://auth:8000"),
    ("/me", "http://auth:8000"),
    ("/users/me/accounts", "http://user:8000"),
    ("/accounts/acc-1/members", "http://user:8000"),
    ("/invites/accept", "http://user:8000"),
    ("/billing/subscription", "http://billing:8000"),
    ("/webhooks/stripe", "http://billing:8000"),
    ("/credits/balance", "http://credits:8000"),
    ("/credits/marketplace/listings", "http://credits:8000"),
    ("/usage/summary", "http://usage:8000"),
    ("/generations", "http://ai:8000"),
    ("/generations/job-1/stream", "http://ai:8000"),
    ("/models", "http://ai:8000"),
    ("/content/c-1/versions", "http://content:8000"),
    ("/schedules/calendar", "http://scheduler:8000"),
    ("/connections", "http://social:8000"),
    ("/publish", "http://social:8000"),
    ("/publish-jobs/j-1", "http://social:8000"),
    ("/scrape-jobs/j-1/documents", "http://scraper:8000"),
    ("/scraped-documents/d-1", "http://scraper:8000"),
    ("/notifications", "http://notification:8000"),
    ("/admin/audit-log", "http://admin:8000"),
]


@pytest.mark.parametrize("path,base", ROUTES, ids=[p for p, _ in ROUTES])
def test_prefix_routes_to_owning_service(client, upstream, compose_urls, owner_auth, path, base):
    r = client.get(path, headers=owner_auth)
    assert r.status_code == 200
    assert str(upstream.requests[0].url) == base + path


def test_route_table_only_names_known_services():
    assert set(proxy.ROUTE_TABLE.values()) == set(proxy.SERVICES)


# ---------------------------------------------------------------------------
# Transparency: method, query, body, headers
# ---------------------------------------------------------------------------

def test_method_query_and_body_preserved(client, upstream, auth):
    r = client.post("/credits/consume?reason=ai&amount=5", json={"tokens": 42}, headers=auth)
    assert r.status_code == 200
    sent = upstream.requests[0]
    assert sent.method == "POST"
    assert sent.url.path == "/credits/consume"
    assert sent.url.query == b"reason=ai&amount=5"
    assert json.loads(sent.content) == {"tokens": 42}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_other_methods_pass_through(client, upstream, auth, method):
    client.request(method, "/content/c-1", headers=auth)
    assert upstream.requests[0].method == method


def test_authorization_forwarded_verbatim(client, upstream):
    # The gateway VERIFIES the token, then forwards the ORIGINAL header
    # untouched so the service verifies it too (defence in depth).
    token = make_token(role="member")
    header = f"Bearer {token}"
    client.get("/credits/balance", headers={"Authorization": header})
    assert upstream.requests[0].headers["authorization"] == header


def test_x_forwarded_for_added(client, upstream, auth):
    client.get("/credits/balance", headers=auth)
    assert upstream.requests[0].headers["x-forwarded-for"] == "testclient"


def test_hop_by_hop_request_headers_dropped(client, upstream, auth):
    client.get(
        "/credits/balance",
        headers={**auth, "Connection": "close", "TE": "trailers"},
    )
    sent = upstream.requests[0].headers
    assert sent.get("connection") != "close"
    assert "te" not in sent


# ---------------------------------------------------------------------------
# Fidelity: upstream status / body / headers come back unchanged
# ---------------------------------------------------------------------------

def test_upstream_status_body_and_headers_returned_faithfully(client, upstream, auth):
    upstream.respond(418, json={"detail": "teapot"}, headers={"X-Custom": "yes"})
    r = client.get("/usage/summary", headers=auth)
    assert r.status_code == 418
    assert r.json() == {"detail": "teapot"}
    assert r.headers["x-custom"] == "yes"


def test_upstream_error_bodies_pass_through(client, upstream, auth):
    upstream.respond(422, json={"detail": [{"loc": ["body", "prompt"], "msg": "field required"}]})
    r = client.post("/generations", json={}, headers=auth)
    assert r.status_code == 422
    assert r.json()["detail"][0]["msg"] == "field required"


# ---------------------------------------------------------------------------
# Gateway error schema
# ---------------------------------------------------------------------------

def test_unknown_prefix_404_and_nothing_forwarded(client, upstream, auth):
    # Unknown routes stay 404 even with a valid token — the gateway doesn't
    # demand credentials for a path it doesn't route.
    r = client.get("/nope/whatever", headers=auth)
    assert r.status_code == 404
    assert "Unknown route" in r.json()["detail"]
    assert upstream.requests == []


def test_root_path_is_unknown_route(client, upstream, auth):
    assert client.get("/", headers=auth).status_code == 404
    assert upstream.requests == []


def test_upstream_down_returns_502(client, upstream, auth):
    upstream.raise_error(httpx.ConnectError("connection refused"))
    r = client.get("/credits/balance", headers=auth)
    assert r.status_code == 502
    assert "credits" in r.json()["detail"]


def test_upstream_timeout_returns_504(client, upstream, auth):
    upstream.raise_error(httpx.ReadTimeout("upstream too slow"))
    r = client.get("/usage/summary", headers=auth)
    assert r.status_code == 504
    assert "usage" in r.json()["detail"]


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

def test_cors_preflight_answered_by_gateway_not_proxied(client, upstream):
    r = client.options(
        "/credits/balance",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200
    # The caller's Origin is ECHOED, not answered with "*": the refresh cookie
    # made these requests credentialed, and a credentialed CORS response with a
    # literal wildcard is rejected by the browser.
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert r.headers["access-control-allow-credentials"] == "true"
    assert "GET" in r.headers["access-control-allow-methods"]
    assert upstream.requests == []  # preflight never reaches an upstream


def test_cors_header_on_simple_request(client, upstream, auth):
    r = client.get("/credits/balance", headers={**auth, "Origin": "http://localhost:5173"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert r.headers["access-control-allow-credentials"] == "true"


def test_multiple_set_cookie_headers_survive_the_proxy(client, upstream, auth):
    """Auth sets TWO cookies on every mint (httpOnly refresh + readable CSRF).
    A dict of headers — or httpx's comma-joining .items() — would fuse them
    into one header and the browser would drop the second cookie."""
    upstream.respond(headers=[
        ("content-type", "application/json"),
        ("set-cookie", "cf_refresh=abc; Path=/auth; HttpOnly; SameSite=strict"),
        ("set-cookie", "cf_csrf=xyz; Path=/; SameSite=strict"),
    ], json={"ok": True})

    r = client.post("/auth/refresh", json={})

    cookies = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    assert len(cookies) == 2
    assert any(c.startswith("cf_refresh=abc") and "HttpOnly" in c for c in cookies)
    assert any(c.startswith("cf_csrf=xyz") for c in cookies)


def test_cookie_header_is_forwarded_upstream(client, upstream):
    """The refresh cookie only works if the gateway passes it to Auth."""
    client.cookies.set("cf_refresh", "the-token")
    client.post("/auth/refresh", json={}, headers={"X-CSRF-Token": "t"})

    forwarded = upstream.requests[-1]
    assert "cf_refresh=the-token" in forwarded.headers["cookie"]
    assert forwarded.headers["x-csrf-token"] == "t"


# ---------------------------------------------------------------------------
# Request IDs
# ---------------------------------------------------------------------------

def test_request_id_generated_tagged_and_forwarded(client, upstream, auth):
    r = client.get("/credits/balance", headers=auth)
    rid = r.headers["x-request-id"]
    assert rid
    assert upstream.requests[0].headers["x-request-id"] == rid


def test_client_supplied_request_id_preserved(client, upstream, auth):
    r = client.get("/credits/balance", headers={**auth, "X-Request-ID": "trace-123"})
    assert r.headers["x-request-id"] == "trace-123"
    assert upstream.requests[0].headers["x-request-id"] == "trace-123"


# ---------------------------------------------------------------------------
# JWT verification (spec §8: verify on every protected route)
# ---------------------------------------------------------------------------

def test_valid_token_is_admitted(client, upstream, auth):
    assert client.get("/credits/balance", headers=auth).status_code == 200
    assert len(upstream.requests) == 1


def test_missing_token_is_401_and_nothing_forwarded(client, upstream):
    r = client.get("/credits/balance")
    assert r.status_code == 401
    assert r.json() == {"detail": "Missing bearer token"}
    assert upstream.requests == []


def test_non_bearer_authorization_is_401(client, upstream):
    r = client.get("/credits/balance", headers={"Authorization": "Basic abc123"})
    assert r.status_code == 401
    assert upstream.requests == []


def test_invalid_signature_is_401(client, upstream):
    forged = make_token(role="member", key=WRONG_PRIVATE_KEY)
    r = client.get("/credits/balance", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    assert "invalid token" in r.json()["detail"]
    assert upstream.requests == []


def test_expired_token_is_401(client, upstream):
    expired = make_token(role="member", ttl_seconds=-60)
    r = client.get("/credits/balance", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert r.json() == {"detail": "token expired"}
    assert upstream.requests == []


def test_refresh_token_rejected_on_protected_route(client, upstream):
    refresh = make_token(role="member", token_type="refresh")
    r = client.get("/credits/balance", headers={"Authorization": f"Bearer {refresh}"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Not an access token"}
    assert upstream.requests == []


def test_wrong_issuer_rejected(client, upstream):
    foreign = make_token(role="member", issuer="evil-issuer")
    r = client.get("/credits/balance", headers={"Authorization": f"Bearer {foreign}"})
    assert r.status_code == 401
    assert upstream.requests == []


def test_gateway_verifies_but_service_still_sees_the_token(client, upstream, auth):
    # Defence in depth: even after the gateway's own 401 gate, the forwarded
    # request still carries the Authorization header for the service to check.
    client.get("/credits/balance", headers=auth)
    assert "authorization" in upstream.requests[0].headers


# ---------------------------------------------------------------------------
# Public routes bypass auth entirely
# ---------------------------------------------------------------------------

PUBLIC_ROUTES = [
    ("POST", "/auth/login"),
    ("POST", "/auth/signup"),
    ("POST", "/auth/verify-email"),
    ("POST", "/auth/refresh"),
    ("POST", "/auth/password-reset/request"),
    ("POST", "/auth/password-reset/confirm"),
]


@pytest.mark.parametrize("method,path", PUBLIC_ROUTES, ids=[p for _, p in PUBLIC_ROUTES])
def test_public_routes_reach_upstream_without_a_token(client, upstream, method, path):
    r = client.request(method, path, json={})
    assert r.status_code == 200
    assert len(upstream.requests) == 1  # forwarded, not blocked at the gateway


def test_health_needs_no_token(client):
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Role enforcement (spec §6: roles enforced at the gateway)
# ---------------------------------------------------------------------------

def test_member_forbidden_from_admin_console(client, upstream):
    r = client.get("/admin/audit-log", headers=bearer("member"))
    assert r.status_code == 403
    assert upstream.requests == []  # refused before it left the gateway


@pytest.mark.parametrize("role", ["owner", "admin", "superadmin"])
def test_admin_console_admits_admin_tier(client, upstream, role):
    r = client.get("/admin/audit-log", headers=bearer(role))
    assert r.status_code == 200
    assert len(upstream.requests) == 1


def test_member_forbidden_from_owner_only_billing(client, upstream):
    r = client.post("/billing/checkout", json={"plan": "pro"}, headers=bearer("member"))
    assert r.status_code == 403
    assert upstream.requests == []


def test_admin_forbidden_from_owner_only_billing(client, upstream):
    # SuperAdmin/admin do NOT bypass the owner-only money gate — mirrors the
    # services, which check role == "owner" literally.
    assert client.post("/billing/checkout", json={}, headers=bearer("admin")).status_code == 403
    assert client.post("/billing/checkout", json={}, headers=bearer("superadmin")).status_code == 403


def test_owner_allowed_through_owner_only_billing(client, upstream):
    r = client.post("/billing/checkout", json={"plan": "pro"}, headers=bearer("owner"))
    assert r.status_code == 200
    assert len(upstream.requests) == 1


def test_member_forbidden_from_content_status_transition(client, upstream):
    r = client.post("/content/c-1/status", json={"status": "approved"}, headers=bearer("member"))
    assert r.status_code == 403
    assert upstream.requests == []


def test_manager_allowed_content_status_transition(client, upstream):
    r = client.post("/content/c-1/status", json={"status": "approved"}, headers=bearer("admin"))
    assert r.status_code == 200


def test_member_forbidden_from_calendar_mutation(client, upstream):
    assert client.post("/schedules", json={}, headers=bearer("member")).status_code == 403
    assert client.delete("/schedules/s-1", headers=bearer("member")).status_code == 403
    assert upstream.requests == []


def test_member_can_read_calendar(client, upstream):
    # GET is not gated — only POST/PATCH/DELETE mutate.
    r = client.get("/schedules/calendar", headers=bearer("member"))
    assert r.status_code == 200


def test_member_forbidden_from_publish_and_connections(client, upstream):
    assert client.post("/publish", json={}, headers=bearer("member")).status_code == 403
    assert client.post("/connections/linkedin/start", json={}, headers=bearer("member")).status_code == 403
    assert upstream.requests == []


def test_member_can_read_connections(client, upstream):
    assert client.get("/connections", headers=bearer("member")).status_code == 200


def test_marketplace_listing_creation_owner_only(client, upstream):
    assert client.post("/credits/marketplace/listings", json={}, headers=bearer("member")).status_code == 403
    assert client.get("/credits/marketplace/listings", headers=bearer("member")).status_code == 200
    assert client.post("/credits/marketplace/listings", json={}, headers=bearer("owner")).status_code == 200


def test_membership_routes_not_role_gated_at_gateway(client, upstream):
    # /accounts/* authorize against the membership TABLE for the path's
    # account, which the gateway can't see — a member's token must pass
    # through so the User service is the authority.
    assert client.get("/accounts/acc-1/members", headers=bearer("member")).status_code == 200
    assert client.patch("/accounts/acc-1", json={}, headers=bearer("member")).status_code == 200


# ---------------------------------------------------------------------------
# Rate limiting — Redis-backed, per-IP and per-account
# ---------------------------------------------------------------------------

def test_per_ip_rate_limit_429_after_threshold(client, upstream, monkeypatch):
    monkeypatch.setattr(ratelimit, "IP_LIMIT_PER_WINDOW", 3)
    for _ in range(3):
        assert client.get("/credits/balance", headers=bearer("member")).status_code == 200
    r = client.get("/credits/balance", headers=bearer("member"))
    assert r.status_code == 429
    assert r.json() == {"detail": "Rate limit exceeded"}
    assert len(upstream.requests) == 3  # the rejected request never went upstream


def test_per_account_rate_limit_429(client, upstream, monkeypatch):
    # Per-account limit bites even across different client IPs / tokens: the
    # counter is keyed on account_id, not the connection.
    monkeypatch.setattr(ratelimit, "ACCOUNT_LIMIT_PER_WINDOW", 2)
    h = bearer("member", account_id="acc-shared")
    assert client.get("/credits/balance", headers=h).status_code == 200
    assert client.get("/credits/balance", headers=h).status_code == 200
    r = client.get("/credits/balance", headers=h)
    assert r.status_code == 429
    assert r.json() == {"detail": "Account rate limit exceeded"}


def test_per_account_limit_isolates_accounts(client, upstream, monkeypatch):
    monkeypatch.setattr(ratelimit, "ACCOUNT_LIMIT_PER_WINDOW", 1)
    assert client.get("/credits/balance", headers=bearer("member", account_id="acc-a")).status_code == 200
    # A different account is on its own counter, unaffected by acc-a.
    assert client.get("/credits/balance", headers=bearer("member", account_id="acc-b")).status_code == 200
    # acc-a is now over its limit.
    assert client.get("/credits/balance", headers=bearer("member", account_id="acc-a")).status_code == 429


def test_health_exempt_from_rate_limit(client, monkeypatch):
    monkeypatch.setattr(ratelimit, "IP_LIMIT_PER_WINDOW", 1)
    for _ in range(5):
        assert client.get("/health").status_code == 200


def test_rate_limit_zero_disables(client, upstream, monkeypatch):
    monkeypatch.setattr(ratelimit, "IP_LIMIT_PER_WINDOW", 0)
    monkeypatch.setattr(ratelimit, "ACCOUNT_LIMIT_PER_WINDOW", 0)
    for _ in range(10):
        assert client.get("/credits/balance", headers=bearer("member")).status_code == 200


def test_rate_limiter_fails_open_when_redis_down(client, upstream, monkeypatch):
    # A protection, not an authorization decision: if the counter store is
    # unreachable the request still flows (webhook dedup, which IS a
    # correctness decision, fails closed instead — see the dedup tests).
    monkeypatch.setattr(ratelimit, "IP_LIMIT_PER_WINDOW", 1)

    class _BrokenRedis:
        def pipeline(self):
            raise OSError("redis down")

    monkeypatch.setattr(main.store, "get_redis", lambda: _BrokenRedis())
    for _ in range(5):
        assert client.get("/credits/balance", headers=bearer("member")).status_code == 200


# ---------------------------------------------------------------------------
# Webhooks: signature verification, dedup, publish, relay
# ---------------------------------------------------------------------------

def _stripe_event(event_id="evt_1", event_type="invoice.paid") -> bytes:
    return json.dumps({
        "id": event_id,
        "type": event_type,
        "data": {"object": {"id": "in_1", "customer": "cus_1", "amount_paid": 500}},
    }).encode("utf-8")


def test_stripe_webhook_valid_signature_publishes_and_relays(client, upstream, published):
    # The gateway answers the provider with BILLING's verdict (it relays), so
    # the endpoint returns Billing's body, not its own — that is the "relay,
    # don't double-process" reconciliation.
    upstream.respond_for("/webhooks/stripe", 200, json={"status": "processed", "note": "invoice paid"})
    payload = _stripe_event()
    sig = signatures.stripe_signature_header(payload, STRIPE_TEST_SECRET)
    r = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 200
    assert r.json()["status"] == "processed"   # Billing's verdict, relayed back

    # Published a normalized billing.* event (single-segment routing key).
    assert len(published) == 1
    routing_key, event_payload, event_id = published[0]
    assert routing_key == "billing.invoice_paid"
    assert event_payload["provider"] == "stripe"
    assert event_payload["provider_event_type"] == "invoice.paid"
    assert event_id == "evt_1"

    # Relayed the ORIGINAL bytes + signature to Billing's own endpoint.
    relayed = upstream.requests[0]
    assert relayed.url.path == "/webhooks/stripe"
    assert relayed.headers["stripe-signature"] == sig
    assert relayed.content == payload


def test_stripe_webhook_tampered_payload_rejected(client, upstream, published):
    payload = _stripe_event()
    sig = signatures.stripe_signature_header(payload, STRIPE_TEST_SECRET)
    tampered = payload.replace(b"500", b"999999")
    r = client.post("/webhooks/stripe", content=tampered, headers={"stripe-signature": sig})
    assert r.status_code == 401
    assert published == []          # nothing published
    assert upstream.requests == []  # nothing relayed


def test_stripe_webhook_wrong_secret_rejected(client, published):
    payload = _stripe_event()
    sig = signatures.stripe_signature_header(payload, "whsec_the_wrong_secret")
    r = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 401
    assert published == []


def test_stripe_webhook_missing_signature_rejected(client, published):
    r = client.post("/webhooks/stripe", content=_stripe_event())
    assert r.status_code == 401
    assert published == []


def test_webhook_dedup_same_event_processed_once(client, upstream, published):
    payload = _stripe_event(event_id="evt_dedup")
    sig = signatures.stripe_signature_header(payload, STRIPE_TEST_SECRET)
    headers = {"stripe-signature": sig}

    first = client.post("/webhooks/stripe", content=payload, headers=headers)
    assert first.status_code == 200          # relayed to Billing (its verdict)

    second = client.post("/webhooks/stripe", content=payload, headers=headers)
    assert second.json()["status"] == "duplicate"   # deduped before any relay

    # Published once, relayed once — the redelivery did neither.
    assert len(published) == 1
    assert len(upstream.requests) == 1


def test_webhook_dedup_released_on_retryable_relay_failure(client, upstream, published):
    # Billing 500 is retryable: the dedup key must be released so Stripe's
    # redelivery is processed, not swallowed.
    upstream.respond_for("/webhooks/stripe", 500, json={"detail": "billing boom"})
    payload = _stripe_event(event_id="evt_retry")
    sig = signatures.stripe_signature_header(payload, STRIPE_TEST_SECRET)
    headers = {"stripe-signature": sig}

    first = client.post("/webhooks/stripe", content=payload, headers=headers)
    assert first.status_code == 500

    # Retry succeeds now that Billing is healthy — proves the key was freed.
    upstream.respond_for("/webhooks/stripe", 200, json={"status": "processed"})
    second = client.post("/webhooks/stripe", content=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "processed"


def test_webhook_dedup_fails_closed_when_redis_down(client, upstream, published, monkeypatch):
    class _BrokenRedis:
        def pipeline(self):
            # The per-IP limiter hits Redis first; it fails OPEN on this.
            raise OSError("redis down")

        async def set(self, *a, **k):
            # The dedup SETNX hits Redis next; it fails CLOSED on this.
            raise OSError("redis down")

    monkeypatch.setattr(webhooks.store, "get_redis", lambda: _BrokenRedis())
    payload = _stripe_event()
    sig = signatures.stripe_signature_header(payload, STRIPE_TEST_SECRET)
    r = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 503          # refuse rather than risk double-processing
    assert published == []
    assert upstream.requests == []


def test_linkedin_webhook_verifies_and_publishes_social_event(client, upstream, published):
    payload = json.dumps({"eventId": "li_1", "eventType": "SHARE_STATUS_UPDATE"}).encode("utf-8")
    sig = signatures.linkedin_signature_header(payload, LINKEDIN_TEST_SECRET)
    r = client.post("/webhooks/linkedin", content=payload, headers={"x-li-signature": sig})
    assert r.status_code == 200
    assert len(published) == 1
    routing_key, _, event_id = published[0]
    assert routing_key.startswith("social.")
    assert event_id == "li_1"
    assert upstream.requests == []  # no relay target for LinkedIn


def test_linkedin_webhook_tampered_rejected(client, published):
    payload = json.dumps({"eventId": "li_2"}).encode("utf-8")
    sig = signatures.linkedin_signature_header(payload, LINKEDIN_TEST_SECRET)
    r = client.post("/webhooks/linkedin", content=payload + b" ", headers={"x-li-signature": sig})
    assert r.status_code == 401
    assert published == []


def test_openrouter_webhook_verifies_and_publishes_ai_event(client, upstream, published):
    payload = json.dumps({"id": "or_1", "type": "generation.completed"}).encode("utf-8")
    sig = signatures.openrouter_signature_header(payload, OPENROUTER_TEST_SECRET)
    r = client.post("/webhooks/openrouter", content=payload, headers={"x-openrouter-signature": sig})
    assert r.status_code == 200
    assert len(published) == 1
    routing_key, _, _ = published[0]
    assert routing_key == "ai.generation_completed"


def test_openrouter_webhook_tampered_rejected(client, published):
    payload = json.dumps({"id": "or_2", "type": "x"}).encode("utf-8")
    sig = signatures.openrouter_signature_header(payload, OPENROUTER_TEST_SECRET)
    r = client.post("/webhooks/openrouter", content=b'{"id":"or_2","type":"y"}',
                    headers={"x-openrouter-signature": sig})
    assert r.status_code == 401
    assert published == []


def test_webhook_without_event_id_dedups_on_body_hash(client, upstream, published):
    # OpenRouter without an id -> stable sha256 identity; a second identical
    # delivery is still deduped.
    payload = json.dumps({"type": "ping"}).encode("utf-8")
    sig = signatures.openrouter_signature_header(payload, OPENROUTER_TEST_SECRET)
    headers = {"x-openrouter-signature": sig}
    assert client.post("/webhooks/openrouter", content=payload, headers=headers).json()["status"] == "processed"
    assert client.post("/webhooks/openrouter", content=payload, headers=headers).json()["status"] == "duplicate"
    assert len(published) == 1


def test_webhooks_are_public_no_bearer_required(client):
    # The webhook ingress authenticates by signature, never by JWT.
    payload = _stripe_event()
    sig = signatures.stripe_signature_header(payload, STRIPE_TEST_SECRET)
    r = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": sig})
    assert r.status_code == 200  # no Authorization header anywhere


# ---------------------------------------------------------------------------
# Response aggregation — GET /dashboard/summary
# ---------------------------------------------------------------------------

def test_dashboard_summary_composes_three_services(client, upstream, auth):
    upstream.respond_for("/accounts/", 200, json={"account_id": "acc-test", "plan_tier": "pro", "seat_count": 4})
    upstream.respond_for("/credits/balance", 200, json={"balance": 120})
    upstream.respond_for("/usage/summary", 200, json={"used_tokens": 3400})

    r = client.get("/dashboard/summary", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == "acc-test"
    assert body["profile"]["plan_tier"] == "pro"
    assert body["credits"]["balance"] == 120
    assert body["usage"]["used_tokens"] == 3400
    assert body["degraded"] == []

    # Fanned out to exactly the three services, each carrying the caller's token.
    paths = sorted(req.url.path for req in upstream.requests)
    assert paths == ["/accounts/acc-test", "/credits/balance", "/usage/summary"]
    assert all("authorization" in req.headers for req in upstream.requests)


def test_dashboard_summary_requires_a_token(client, upstream):
    assert client.get("/dashboard/summary").status_code == 401
    assert upstream.requests == []


def test_dashboard_summary_degrades_per_section(client, upstream, auth):
    upstream.respond_for("/accounts/", 200, json={"account_id": "acc-test", "plan_tier": "free"})
    upstream.respond_for("/credits/balance", 503, json={"detail": "credits down"})
    upstream.respond_for("/usage/summary", 200, json={"used_tokens": 10})

    r = client.get("/dashboard/summary", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["profile"]["plan_tier"] == "free"
    assert body["credits"] is None
    assert body["degraded"] == ["credits"]


# ---------------------------------------------------------------------------
# Health / readiness
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "gateway"}


def test_health_upstreams_all_ok(client, upstream):
    r = client.get("/health/upstreams")
    body = r.json()
    assert set(body["upstreams"]) == set(proxy.SERVICES)
    assert all(v == "ok" for v in body["upstreams"].values())
    probed = {str(req.url) for req in upstream.requests}
    assert all(url.endswith("/health") for url in probed)


def test_health_upstreams_reports_down(client, upstream):
    upstream.raise_error(httpx.ConnectError("connection refused"))
    r = client.get("/health/upstreams")
    assert r.status_code == 200
    assert all(v == "down" for v in r.json()["upstreams"].values())


# ---------------------------------------------------------------------------
# SSE passthrough — the critical path (kept exactly, now with a real token)
# ---------------------------------------------------------------------------

SSE_CHUNKS = [
    b'event: token\ndata: {"seq": 1, "content": "Hello"}\n\n',
    b'event: token\ndata: {"seq": 2, "content": " "}\n\n',
    b'event: token\ndata: {"seq": 3, "content": "world"}\n\n',
    b'event: done\ndata: {"seq": 4, "total_tokens": 10}\n\n',
]

SSE_HEADERS = {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache",
    "x-accel-buffering": "no",
}


def _install_sse_upstream(upstream, yielded: list | None = None):
    async def stream():
        for i, chunk in enumerate(SSE_CHUNKS):
            if yielded is not None:
                yielded.append(i)
            yield chunk

    upstream.respond(200, headers=SSE_HEADERS, stream_factory=stream)


def test_sse_chunks_arrive_in_order_with_stream_headers(client, upstream):
    _install_sse_upstream(upstream)
    token = make_token(role="member")
    received = []
    with client.stream(
        "GET", "/generations/job-1/stream", headers={"Authorization": f"Bearer {token}"}
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-accel-buffering"] == "no"
        assert "content-length" not in r.headers
        for chunk in r.iter_raw():
            received.append(chunk)
    # TestClient buffers the ASGI body into one read, so boundaries can't be
    # observed here — full payload + order is asserted end-to-end, and
    # chunk-by-chunk laziness is proven by the direct test below.
    assert b"".join(received) == b"".join(SSE_CHUNKS)
    assert upstream.requests[0].headers["authorization"] == f"Bearer {token}"


def test_sse_upstream_consumed_lazily_not_buffered(upstream):
    """Drive proxy.forward directly and pull the response iterator one chunk
    at a time: the fake upstream generator must advance in lock-step with the
    reads. If the gateway buffered the body, all four chunks would be
    consumed before the first read completed."""

    async def run():
        yielded: list[int] = []
        _install_sse_upstream(upstream, yielded)

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/generations/job-1/stream",
            "raw_path": b"/generations/job-1/stream",
            "query_string": b"",
            "headers": [(b"host", b"gateway"), (b"authorization", b"Bearer tok")],
            "client": ("127.0.0.1", 1234),
            "server": ("gateway", 8000),
            "state": {},
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        response = await proxy.forward(Request(scope, receive))
        assert response.status_code == 200

        iterator = response.body_iterator
        first = await iterator.__anext__()
        assert first == SSE_CHUNKS[0]
        assert yielded == [0]          # nothing pre-read beyond what we consumed
        second = await iterator.__anext__()
        assert second == SSE_CHUNKS[1]
        assert yielded == [0, 1]
        rest = [chunk async for chunk in iterator]
        assert [first, second] + rest == SSE_CHUNKS
        assert yielded == [0, 1, 2, 3]

    asyncio.run(run())


def test_sse_stream_request_gets_unbounded_read_timeout(client, upstream):
    _install_sse_upstream(upstream)
    auth = {"Authorization": f"Bearer {make_token(role='member')}"}
    with client.stream("GET", "/generations/job-1/stream", headers=auth) as r:
        r.read()
    timeout = upstream.requests[0].extensions["timeout"]
    assert timeout["read"] is None      # a live token stream must never be cut off
    client.get("/credits/balance", headers=auth)
    assert upstream.requests[1].extensions["timeout"]["read"] == proxy.READ_TIMEOUT
