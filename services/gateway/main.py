"""
CreditFlow API Gateway — the single public entry point (host :8080) in front
of all twelve internal services.

Responsibilities (spec §8 Service 1, §6):
  - Route every public path prefix to the owning service (proxy.py), with the
    unbuffered SSE passthrough that carries the AI token stream intact.
  - Verify the RS256 access token on every PROTECTED route and enforce the
    role each route requires (security.py) — the first line of defence;
    services still verify the forwarded token themselves.
  - Redis-backed rate limiting, per-IP and per-account (ratelimit.py) — all
    gateway state lives in Redis (spec Database Ownership: "None — stateless").
  - Provider webhook ingress: verify signature, dedup in Redis, publish the
    normalized event to RabbitMQ, relay Stripe to Billing (webhooks.py).
  - Response aggregation for multi-service screens (aggregate.py).
  - The cross-cutting concerns from before: CORS, X-Request-ID, health, and
    the consistent {detail} error schema.

MIDDLEWARE PIPELINE — order matters and is built inside-out (the LAST
`add_middleware` runs OUTERMOST). Reading outermost -> innermost as a request
arrives:

    CORS                 answer preflights; put CORS headers on every response
      -> request-id/log  tag + time the request; every rejection is logged too
        -> rate limit    per-IP shield BEFORE auth (cheap, pre-identity)
          -> auth        verify JWT + enforce role on protected routes, then
                         the per-ACCOUNT rate limit once identity is known
            -> route     webhooks / dashboard / health / catch-all proxy

The per-IP limit sits OUTSIDE auth so a flood of forged tokens is throttled
before it costs an RSA verification each; the per-account limit lives INSIDE
auth because there is no account to key on until the token is read. Public
routes skip auth AND the per-account limit (there is no account yet) but still
pass the per-IP shield. /health is exempt from everything so healthchecks and
the compose dependency graph never trip a limiter.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

import aggregate  # noqa: E402
import proxy  # noqa: E402
import ratelimit  # noqa: E402
import security  # noqa: E402
import store  # noqa: E402
import webhooks  # noqa: E402

SERVICE_NAME = os.getenv("SERVICE_NAME", "gateway")

logger = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Release the three long-lived resources the gateway now holds.
    await proxy.close_client()
    await store.close_redis()
    import events
    events.close()


app = FastAPI(title=f"CreditFlow — {SERVICE_NAME}", version="0.2.0", lifespan=lifespan)


def _error(status_code: int, detail: str, request_id: str | None = None) -> JSONResponse:
    """The one error shape the gateway emits — matches the services' {detail}
    so a client sees a single vocabulary whoever refused."""
    headers = {"x-request-id": request_id} if request_id else None
    return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _exempt(path: str) -> bool:
    """/health and /health/upstreams never touch a limiter or auth."""
    path = security.normalize(path)
    return path == "/health" or path.startswith("/health/")


# ---------------------------------------------------------------------------
# Auth + per-account rate limit (INNERMOST of the custom middlewares).
# Runs after the per-IP shield, so identity work is already rate-protected.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def authenticate_and_authorize(request: Request, call_next):
    path = request.url.path
    request_id = getattr(request.state, "request_id", None)

    if request.method == "OPTIONS" or _exempt(path) or security.is_public(path):
        return await call_next(request)

    # An unknown route is a 404 (proxy's job), not a 401 — don't demand a
    # token for a path that isn't routed anywhere.
    if not proxy.is_known_path(path):
        return await call_next(request)

    try:
        claims = security.authenticate(
            request.method, path, request.headers.get("authorization", "")
        )
    except security.AuthError as exc:
        return _error(exc.status_code, exc.detail, request_id)

    request.state.claims = claims

    # Per-account limit — now that we know the account (spec: per-account).
    if not await ratelimit.allow_account(claims["account_id"]):
        return _error(429, "Account rate limit exceeded", request_id)

    return await call_next(request)


# ---------------------------------------------------------------------------
# Per-IP rate limit (the anonymous shield). Wraps auth, so it runs first.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def rate_limit_per_ip(request: Request, call_next):
    if request.method == "OPTIONS" or _exempt(request.url.path):
        return await call_next(request)
    request_id = getattr(request.state, "request_id", None)
    if not await ratelimit.allow_ip(_client_ip(request)):
        return _error(429, "Rate limit exceeded", request_id)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Request ID + access log. Wraps the limiters (defined after them), so 429s
# and 401/403s are tagged and logged too.
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


# CORS added last = outermost, so preflights short-circuit before anything
# else and even 429/401/403 carry CORS headers.
#
# allow_credentials is now ON because the refresh token travels as an httpOnly
# cookie (auth/cookies.py) and the frontend is a different ORIGIN from the
# gateway in dev (:5173 vs :8080) — without it the browser would neither send
# that cookie nor expose the Set-Cookie response.
#
# Credentialed CORS forbids the literal `Access-Control-Allow-Origin: *`, so
# the wildcard is expressed as an echo-any-origin regex instead: Starlette then
# reflects the caller's exact Origin, which is what the browser requires. That
# is deliberately permissive and meant for dev — GATEWAY_CORS_ORIGINS should
# name the real frontend origins in any deployment, and the log line below
# says so out loud. The credential itself is not left leaning on CORS: the
# refresh cookie is SameSite=strict and the refresh route wants a
# double-submit CSRF header, both of which hold regardless of this setting.
_cors_origins = [
    o.strip() for o in os.getenv("GATEWAY_CORS_ORIGINS", "*").split(",") if o.strip()
]
if "*" in _cors_origins:
    logger.warning(
        "GATEWAY_CORS_ORIGINS is '*' — every origin is allowed to make credentialed "
        "requests. Set it to the frontend origin(s) outside local development."
    )
    _cors_kwargs = {"allow_origin_regex": ".*"}
else:
    _cors_kwargs = {"allow_origins": _cors_origins}

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    **_cors_kwargs,
)


# Gateway-owned routers, registered BEFORE the catch-all so their concrete
# paths win over /{path:path}.
app.include_router(webhooks.router)
app.include_router(aggregate.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/health/upstreams")
async def health_upstreams() -> dict:
    """Readiness view: probe every upstream's /health concurrently."""
    import asyncio

    client = proxy.get_client()

    async def probe(name: str, base: str) -> tuple[str, str]:
        try:
            r = await client.get(base + "/health", timeout=httpx.Timeout(2.0))
            return name, "ok" if r.status_code == 200 else f"unhealthy ({r.status_code})"
        except httpx.HTTPError:
            return name, "down"

    results = await asyncio.gather(*(probe(n, b) for n, b in proxy.SERVICES.items()))
    return {"status": "ok", "service": SERVICE_NAME, "upstreams": dict(results)}


# Catch-all LAST so concrete routes win first-match. Everything else is proxied.
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@app.api_route("/{path:path}", methods=_METHODS, include_in_schema=False)
async def gateway(path: str, request: Request):
    return await proxy.forward(request)
