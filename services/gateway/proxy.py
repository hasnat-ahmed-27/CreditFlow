"""
Transparent reverse proxy core.

The route table maps the FIRST segment of the public path to the owning
service (each service declares full paths like /credits/balance, so one
segment is enough to pick the upstream). The proxy forwards the method,
path, query string, body, and headers — Authorization passes through
UNTOUCHED: the gateway never verifies or mints JWTs, every service checks
the RS256 access token itself with the shared public key (see the Auth
service docstring for why only Auth holds the private key).

Responses stream back chunk-by-chunk via httpx `send(stream=True)` +
StreamingResponse. That is what makes the AI service's
GET /generations/{id}/stream SSE passthrough work: each token chunk is
yielded to the browser the moment the upstream produces it — nothing is
buffered (the AI service already sends X-Accel-Buffering: no for exactly
this reason). Non-SSE responses take the same path, which keeps the proxy
one code path and byte-faithful (aiter_raw never decodes, so
Content-Encoding/Content-Length pass through intact).
"""
from __future__ import annotations

import os

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask


def _env_url(var: str, default: str) -> str:
    return os.getenv(var, default).rstrip("/")


# Upstream base URLs — in-network compose names by default, overridable per
# environment (tests point them all at a dead address).
SERVICES: dict[str, str] = {
    "auth": _env_url("AUTH_URL", "http://auth:8000"),
    "user": _env_url("USER_URL", "http://user:8000"),
    "billing": _env_url("BILLING_URL", "http://billing:8000"),
    "credits": _env_url("CREDITS_URL", "http://credits:8000"),
    "usage": _env_url("USAGE_URL", "http://usage:8000"),
    "ai": _env_url("AI_URL", "http://ai:8000"),
    "content": _env_url("CONTENT_URL", "http://content:8000"),
    "scheduler": _env_url("SCHEDULER_URL", "http://scheduler:8000"),
    "social": _env_url("SOCIAL_URL", "http://social:8000"),
    "scraper": _env_url("SCRAPER_URL", "http://scraper:8000"),
    "notification": _env_url("NOTIFICATION_URL", "http://notification:8000"),
    "admin": _env_url("ADMIN_URL", "http://admin:8000"),
}

# Public path prefix (first segment) -> owning service. Derived from each
# service's actual route declarations, not guessed.
ROUTE_TABLE: dict[str, str] = {
    "auth": "auth",
    "me": "auth",                    # token introspection lives on the Auth app
    "users": "user",
    "accounts": "user",
    "invites": "user",
    "billing": "billing",
    "webhooks": "billing",           # Stripe webhooks — Billing verifies the signature
    "credits": "credits",
    "usage": "usage",
    "generations": "ai",
    "models": "ai",
    "content": "content",
    "schedules": "scheduler",
    "connections": "social",
    "publish": "social",
    "publish-jobs": "social",
    "scrape-jobs": "scraper",
    "scraped-documents": "scraper",
    "notifications": "notification",
    "admin": "admin",
}

CONNECT_TIMEOUT = float(os.getenv("GATEWAY_CONNECT_TIMEOUT_SECONDS", "5"))
READ_TIMEOUT = float(os.getenv("GATEWAY_READ_TIMEOUT_SECONDS", "30"))

# RFC 9110 hop-by-hop headers — meaningful only for one connection, never
# forwarded in either direction.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}
_REQUEST_DROP = HOP_BY_HOP | {"host", "content-length"}  # httpx sets both itself
_RESPONSE_DROP = HOP_BY_HOP | {"date", "server"}         # our server layer adds its own

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Lazily-created shared client (connection pooling across requests).
    Tests monkeypatch this to return a MockTransport-backed client."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(follow_redirects=False)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _is_sse(path: str) -> bool:
    # The AI stream endpoint is the one long-lived response; it must not be
    # killed by the ordinary read timeout.
    return path.rstrip("/").endswith("/stream")


async def forward(request: Request) -> Response:
    """Proxy one request to the upstream that owns its path prefix."""
    segment = request.url.path.strip("/").split("/", 1)[0]
    service = ROUTE_TABLE.get(segment)
    if service is None:
        return JSONResponse({"detail": f"Unknown route: /{segment}"}, status_code=404)
    base = SERVICES[service]

    url = base + request.url.path
    if request.url.query:
        url += "?" + request.url.query

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _REQUEST_DROP}
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        headers["x-request-id"] = request_id
    if request.client:
        prior = headers.get("x-forwarded-for")
        headers["x-forwarded-for"] = (
            f"{prior}, {request.client.host}" if prior else request.client.host
        )

    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=None if _is_sse(request.url.path) else READ_TIMEOUT,
        write=READ_TIMEOUT,
        pool=CONNECT_TIMEOUT,
    )

    client = get_client()
    upstream_request = client.build_request(
        request.method, url, headers=headers, content=await request.body(), timeout=timeout,
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException:
        return JSONResponse({"detail": f"Upstream timeout: {service}"}, status_code=504)
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"detail": f"Upstream unavailable: {service} ({exc.__class__.__name__})"},
            status_code=502,
        )

    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _RESPONSE_DROP
    }
    return StreamingResponse(
        upstream.aiter_raw(),                       # raw bytes, chunk-by-chunk, no decode
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream.aclose),  # release the connection after the last chunk
    )
