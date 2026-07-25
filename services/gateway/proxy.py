"""
Transparent reverse proxy core.

The route table maps the FIRST segment of the public path to the owning
service (each service declares full paths like /credits/balance, so one
segment is enough to pick the upstream). The proxy forwards the method,
path, query string, body, and headers — Authorization passes through
UNTOUCHED. The gateway now VERIFIES that token first (security.py) but still
forwards the original header rather than any assertion of its own, so every
service keeps checking the RS256 signature itself with the shared public key:
defence in depth, and the gateway still cannot mint identity (only Auth holds
the private key).

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
    # /webhooks/{stripe,linkedin,openrouter} are served BY the gateway now
    # (webhooks.py, declared ahead of the catch-all). This entry is the
    # fallback for any other /webhooks/* path and records who owns them.
    "webhooks": "billing",
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

# First segments the gateway answers ITSELF rather than proxying: health and
# readiness, the provider webhook ingress (webhooks.py), and the composed
# dashboard view (aggregate.py). Listed here so is_known_path() below counts
# them as routed.
GATEWAY_OWNED_SEGMENTS: frozenset[str] = frozenset({"health", "webhooks", "dashboard"})

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


def first_segment(path: str) -> str:
    return path.strip("/").split("/", 1)[0]


def is_known_path(path: str) -> bool:
    """Does the gateway route this path at all?

    The auth middleware asks this so an unknown prefix keeps answering 404
    (the proxy's verdict) instead of 401 — we don't demand credentials for a
    route that does not exist, and the route table is public repo content
    anyway, so nothing is protected by hiding it.
    """
    segment = first_segment(path)
    return segment in ROUTE_TABLE or segment in GATEWAY_OWNED_SEGMENTS


def upstream_headers(request: Request) -> dict[str, str]:
    """The headers to send on the gateway->upstream hop: the client's, minus
    hop-by-hop and the two httpx sets itself, plus our request-id tag and the
    appended X-Forwarded-For. Shared by the proxy and by the webhook relay so
    both hops look identical to the receiving service."""
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _REQUEST_DROP}
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        headers["x-request-id"] = request_id
    if request.client:
        prior = headers.get("x-forwarded-for")
        headers["x-forwarded-for"] = (
            f"{prior}, {request.client.host}" if prior else request.client.host
        )
    return headers


async def forward(request: Request) -> Response:
    """Proxy one request to the upstream that owns its path prefix."""
    segment = first_segment(request.url.path)
    service = ROUTE_TABLE.get(segment)
    if service is None:
        return JSONResponse({"detail": f"Unknown route: /{segment}"}, status_code=404)
    base = SERVICES[service]

    url = base + request.url.path
    if request.url.query:
        url += "?" + request.url.query

    headers = upstream_headers(request)

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

    # Set-Cookie is the one header that may legitimately repeat, and a dict —
    # or httpx's own comma-joining .items() — would collapse the repeats into
    # a single malformed header. Auth sends TWO on every mint (the httpOnly
    # refresh cookie and the readable CSRF cookie), so they are pulled out
    # here and re-attached individually below.
    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _RESPONSE_DROP and k.lower() != "set-cookie"
    }
    response = StreamingResponse(
        upstream.aiter_raw(),                       # raw bytes, chunk-by-chunk, no decode
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream.aclose),  # release the connection after the last chunk
    )
    for key, value in upstream.headers.multi_items():
        if key.lower() == "set-cookie":
            response.raw_headers.append((b"set-cookie", value.encode("latin-1")))
    return response
