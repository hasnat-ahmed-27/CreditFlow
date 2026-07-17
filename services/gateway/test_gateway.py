"""
Gateway suite — runs with plain pytest, no infra, no network (see
conftest.py: every upstream is a MockTransport; real URLs point at a dead
address).

Covers: prefix -> upstream routing for every public route family,
method/path/query/body preservation, verbatim Authorization passthrough,
faithful upstream status/body/headers, the gateway error schema (404
unknown route, 502 upstream down, 504 upstream timeout), CORS, X-Request-ID
tagging, per-IP rate limiting, health/readiness, and — the load-bearing
one — SSE passthrough: chunks arrive in order AND the upstream is only
consumed as fast as the client reads (no buffering).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

import main
import proxy

# ---------------------------------------------------------------------------
# Routing: every public prefix -> the owning service (compose topology)
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
def test_prefix_routes_to_owning_service(client, upstream, compose_urls, path, base):
    r = client.get(path)
    assert r.status_code == 200
    assert str(upstream.requests[0].url) == base + path


def test_route_table_only_names_known_services():
    assert set(proxy.ROUTE_TABLE.values()) == set(proxy.SERVICES)


# ---------------------------------------------------------------------------
# Transparency: method, query, body, headers
# ---------------------------------------------------------------------------

def test_method_query_and_body_preserved(client, upstream):
    r = client.post("/credits/consume?reason=ai&amount=5", json={"tokens": 42})
    assert r.status_code == 200
    sent = upstream.requests[0]
    assert sent.method == "POST"
    assert sent.url.path == "/credits/consume"
    assert sent.url.query == b"reason=ai&amount=5"
    assert json.loads(sent.content) == {"tokens": 42}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_other_methods_pass_through(client, upstream, method):
    client.request(method, "/content/c-1")
    assert upstream.requests[0].method == method


def test_authorization_forwarded_verbatim(client, upstream):
    token = "Bearer eyJhbGciOiJSUzI1NiJ9.payload.signature"
    client.get("/credits/balance", headers={"Authorization": token})
    assert upstream.requests[0].headers["authorization"] == token


def test_gateway_never_blocks_on_auth_itself(client, upstream):
    # No token: the gateway forwards anyway; the upstream's 401 is the
    # verdict the client sees. (Services verify RS256 — spec override.)
    upstream.respond(401, json={"detail": "Missing bearer token"})
    r = client.get("/credits/balance")
    assert r.status_code == 401
    assert r.json() == {"detail": "Missing bearer token"}


def test_x_forwarded_for_added(client, upstream):
    client.get("/credits/balance")
    assert upstream.requests[0].headers["x-forwarded-for"] == "testclient"


def test_hop_by_hop_request_headers_dropped(client, upstream):
    # The client's hop-by-hop values must not be forwarded. httpx puts its
    # own Connection header on the gateway->upstream hop (that hop's own
    # business), so assert the CLIENT's values didn't leak through.
    client.get("/credits/balance", headers={"Connection": "close", "TE": "trailers"})
    sent = upstream.requests[0].headers
    assert sent.get("connection") != "close"
    assert "te" not in sent


# ---------------------------------------------------------------------------
# Fidelity: upstream status / body / headers come back unchanged
# ---------------------------------------------------------------------------

def test_upstream_status_body_and_headers_returned_faithfully(client, upstream):
    upstream.respond(418, json={"detail": "teapot"}, headers={"X-Custom": "yes"})
    r = client.get("/usage/summary")
    assert r.status_code == 418
    assert r.json() == {"detail": "teapot"}
    assert r.headers["x-custom"] == "yes"


def test_upstream_error_bodies_pass_through(client, upstream):
    upstream.respond(422, json={"detail": [{"loc": ["body", "prompt"], "msg": "field required"}]})
    r = client.post("/generations", json={})
    assert r.status_code == 422
    assert r.json()["detail"][0]["msg"] == "field required"


# ---------------------------------------------------------------------------
# Gateway error schema
# ---------------------------------------------------------------------------

def test_unknown_prefix_404_and_nothing_forwarded(client, upstream):
    r = client.get("/nope/whatever")
    assert r.status_code == 404
    assert "Unknown route" in r.json()["detail"]
    assert upstream.requests == []


def test_root_path_is_unknown_route(client, upstream):
    assert client.get("/").status_code == 404
    assert upstream.requests == []


def test_upstream_down_returns_502(client, upstream):
    upstream.raise_error(httpx.ConnectError("connection refused"))
    r = client.get("/credits/balance")
    assert r.status_code == 502
    assert "credits" in r.json()["detail"]


def test_upstream_timeout_returns_504(client, upstream):
    upstream.raise_error(httpx.ReadTimeout("upstream too slow"))
    r = client.get("/usage/summary")
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
    assert r.headers["access-control-allow-origin"] == "*"
    assert "GET" in r.headers["access-control-allow-methods"]
    assert upstream.requests == []  # preflight never reaches an upstream


def test_cors_header_on_simple_request(client, upstream):
    r = client.get("/credits/balance", headers={"Origin": "http://localhost:5173"})
    assert r.headers["access-control-allow-origin"] == "*"


# ---------------------------------------------------------------------------
# Request IDs
# ---------------------------------------------------------------------------

def test_request_id_generated_tagged_and_forwarded(client, upstream):
    r = client.get("/credits/balance")
    rid = r.headers["x-request-id"]
    assert rid
    assert upstream.requests[0].headers["x-request-id"] == rid


def test_client_supplied_request_id_preserved(client, upstream):
    r = client.get("/credits/balance", headers={"X-Request-ID": "trace-123"})
    assert r.headers["x-request-id"] == "trace-123"
    assert upstream.requests[0].headers["x-request-id"] == "trace-123"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_rate_limit_429_after_threshold(client, upstream, monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 3)
    for _ in range(3):
        assert client.get("/credits/balance").status_code == 200
    r = client.get("/credits/balance")
    assert r.status_code == 429
    assert r.json() == {"detail": "Rate limit exceeded"}
    assert r.headers["retry-after"] == "60"
    assert len(upstream.requests) == 3  # the rejected request never went upstream


def test_health_exempt_from_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 1)
    for _ in range(5):
        assert client.get("/health").status_code == 200


def test_rate_limit_zero_disables(client, upstream, monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 0)
    for _ in range(10):
        assert client.get("/credits/balance").status_code == 200


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
# SSE passthrough — the critical path
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
    received = []
    with client.stream(
        "GET", "/generations/job-1/stream", headers={"Authorization": "Bearer tok"}
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
    assert upstream.requests[0].headers["authorization"] == "Bearer tok"


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
    with client.stream("GET", "/generations/job-1/stream") as r:
        r.read()
    timeout = upstream.requests[0].extensions["timeout"]
    assert timeout["read"] is None      # a live token stream must never be cut off
    client.get("/credits/balance")
    assert upstream.requests[1].extensions["timeout"]["read"] == proxy.READ_TIMEOUT
