"""
CreditFlow API Gateway — the single public entry point (host :8080) in front
of all twelve internal services.

One sentence of design: this is a transparent reverse proxy (see proxy.py
for the route table and the streaming passthrough that carries the AI SSE
token stream) plus the spec's cross-cutting concerns — CORS for the
frontend, per-IP rate limiting, and X-Request-ID tagging/logging. It holds
no state and no keys: services verify their own RS256 tokens, so the
Authorization header flows through untouched and the gateway can neither
mint nor forge identity.

Rate limiting is an in-process sliding window rather than the spec's Redis
token-bucket: the gateway is the one service with no infra dependencies
(tests and CI run it bare), and a single gateway process is the deployment
unit here. Redis counters are the drop-in upgrade when the gateway is ever
scaled horizontally.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

import proxy  # noqa: E402

SERVICE_NAME = os.getenv("SERVICE_NAME", "gateway")

logger = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await proxy.close_client()


app = FastAPI(title=f"CreditFlow — {SERVICE_NAME}", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Rate limiting — per-client-IP sliding window (spec §Service 1).
# 0 disables. /health stays exempt so container healthchecks and the
# compose dependency graph never get throttled.
# ---------------------------------------------------------------------------

RATE_LIMIT_PER_MINUTE = int(os.getenv("GATEWAY_RATE_LIMIT_PER_MINUTE", "120"))
_RATE_WINDOW_SECONDS = 60.0
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    limit = RATE_LIMIT_PER_MINUTE  # read per-request so tests can dial it down
    path = request.url.path
    if limit <= 0 or path == "/health" or path.startswith("/health/"):
        return await call_next(request)
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _rate_buckets[ip]
    while bucket and now - bucket[0] > _RATE_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= limit:
        return JSONResponse(
            {"detail": "Rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    bucket.append(now)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Request ID + access log. Wraps the rate limiter (defined after it, so it
# runs outside it) — 429s get logged and tagged too.
# ---------------------------------------------------------------------------


@app.middleware("http")
async def request_id_and_log(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    start = time.monotonic()
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    logger.info(
        "%s %s -> %d (%.1f ms) request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        (time.monotonic() - start) * 1000,
        request_id,
    )
    return response


# CORS added last = outermost, so preflights short-circuit before rate
# limiting and even 429s carry CORS headers. Same posture as the individual
# services (allow-all by default), narrowed to the frontend origin via env.
_cors_origins = [
    o.strip() for o in os.getenv("GATEWAY_CORS_ORIGINS", "*").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/health/upstreams")
async def health_upstreams() -> dict:
    """Readiness view: probe every upstream's /health concurrently."""
    client = proxy.get_client()

    async def probe(name: str, base: str) -> tuple[str, str]:
        try:
            r = await client.get(base + "/health", timeout=httpx.Timeout(2.0))
            return name, "ok" if r.status_code == 200 else f"unhealthy ({r.status_code})"
        except httpx.HTTPError:
            return name, "down"

    results = await asyncio.gather(*(probe(n, b) for n, b in proxy.SERVICES.items()))
    return {"status": "ok", "service": SERVICE_NAME, "upstreams": dict(results)}


# Catch-all LAST so /health wins first-match. Everything else is proxied.
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@app.api_route("/{path:path}", methods=_METHODS, include_in_schema=False)
async def gateway(path: str, request: Request):
    return await proxy.forward(request)
