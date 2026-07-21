"""
Inbound provider webhooks (spec §8 Service 1): "Expose webhook endpoints for
Stripe, LinkedIn, and OpenRouter; verify signatures before acting",
"Deduplicate inbound webhook events using a Redis SETNX key on event ID (24h
TTL)", "Publish normalized events to RabbitMQ after successful webhook
verification and dedup".

PIPELINE — identical for all three providers, one step per spec clause:

  1. read the RAW body once (signatures are over these exact bytes)
  2. verify the provider signature            -> 401 on failure, nothing acted on
  3. SETNX gateway:webhook:<provider>:<id>    -> 200 {"status": "duplicate"} if taken
  4. publish the normalized event to RabbitMQ
  5. (Stripe only) relay the untouched request to the Billing service
  6. release the dedup key if 5 concluded RETRYABLY, so the provider's retry
     is processed rather than swallowed

The dedup key is the ONLY reason step 6 exists. A key set in step 3 and left
behind after a failed relay would turn a redelivery into a silent no-op —
the event would be lost with a 5xx already returned to the provider. Deleting
it on a retryable failure keeps "dedup" from meaning "drop".

STRIPE — WHO VERIFIES, WHO PROCESSES (the reconciliation)
---------------------------------------------------------
Both, deliberately, and only ONE mutates state:

  Gateway   verifies the signature, dedups, publishes `billing.<type>` to
            `billing_events`, then RELAYS the original bytes AND the original
            `Stripe-Signature` header to the Billing service's existing
            POST /webhooks/stripe.
  Billing   re-verifies that same header and runs its persist-before-process
            + transactional outbox (services/billing/webhooks.py) unchanged.

Why not the alternative (gateway verifies, Billing grows a RabbitMQ
consumer): Billing's spec contract is "Consumes: none — receives via
Gateway-relayed webhook events", and its reliability design writes the raw
event to `subscription_events` BEFORE processing. Relaying the raw request
preserves that write verbatim; converting to a queue message would have
thrown the raw payload away and forced a second idempotency mechanism next to
the one Billing already has.

Nothing is double-consumed: the relay carries the SAME event Billing would
have received directly, and the gateway's RabbitMQ publish uses a
namespace Billing consumes nothing from (`billing.invoice_paid`, not
`invoice.paid` — see events.py). The gateway's event is an ANNOUNCEMENT for
the Admin audit log and notifications; Billing's outbox remains the one
source of `invoice.paid`. Nothing is skipped either: verification happens
twice on purpose, exactly like JWT verification at both the gateway and the
service. Verification is a pure function of (bytes, header, secret) — running
it twice consumes nothing.

LinkedIn and OpenRouter have no downstream webhook endpoint to relay to, so
they stop at the publish: `social.*` and `ai.*` land on their domain
exchanges for whoever binds them.

FAILS CLOSED on a Redis outage — unlike rate limiting. Without dedup we
cannot promise a redelivery won't be processed twice, and for a money event
that is a worse answer than 503-and-retry.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx
import redis.exceptions
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from creditflow_common import config

import events
import proxy
import signatures
import store

logger = logging.getLogger("gateway.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

DEDUP_KEY = "gateway:webhook:{provider}:{event_id}"
DEDUP_TTL_SECONDS = 24 * 60 * 60      # spec: 24h TTL

STRIPE_WEBHOOK_SECRET = config.env("STRIPE_WEBHOOK_SECRET", "")
# LinkedIn signs with the app's client secret; a dedicated override exists for
# deployments that rotate the webhook secret independently.
LINKEDIN_WEBHOOK_SECRET = config.env("LINKEDIN_WEBHOOK_SECRET", "") or config.env(
    "LINKEDIN_CLIENT_SECRET", ""
)
OPENROUTER_WEBHOOK_SECRET = config.env("OPENROUTER_WEBHOOK_SECRET", "")

RELAY_TIMEOUT_SECONDS = float(config.env("GATEWAY_WEBHOOK_RELAY_TIMEOUT_SECONDS", "20"))


def _flatten(event_type: str) -> str:
    """`invoice.paid` -> `invoice_paid`. See events.py: the spec's
    `billing.*` contract only matches single-segment keys."""
    return event_type.replace(".", "_").strip("_") or "unknown"


@dataclass(frozen=True)
class Provider:
    """Everything that differs between the three endpoints. The pipeline
    itself (`_receive`) is shared, so adding a fourth provider is one row."""
    name: str
    domain: str                                   # routing-key domain: billing | social | ai
    signature_header: str
    verify: Callable[[bytes, str, str], None]
    event_id: Callable[[dict], str | None]
    event_type: Callable[[dict], str]
    data: Callable[[dict], dict]
    secret_var: str                               # module global holding the secret
    relay_to: tuple[str, str] | None = None       # (service, path)

    @property
    def secret(self) -> str:
        """Resolved per request, not frozen into the table, so a test (or a
        config reload) can rebind the module global and be seen."""
        return globals()[self.secret_var]

PROVIDERS: dict[str, Provider] = {
    "stripe": Provider(
        name="stripe",
        domain="billing",
        signature_header="stripe-signature",
        verify=signatures.verify_stripe,
        event_id=lambda body: body.get("id"),
        event_type=lambda body: body.get("type") or "unknown",
        # Stripe wraps the domain object in data.object.
        data=lambda body: (body.get("data") or {}).get("object") or {},
        secret_var="STRIPE_WEBHOOK_SECRET",
        relay_to=("billing", "/webhooks/stripe"),
    ),
    "linkedin": Provider(
        name="linkedin",
        domain="social",
        signature_header="x-li-signature",
        verify=signatures.verify_linkedin,
        # LinkedIn Event Notifications carry no universal id field; accept the
        # common spellings and fall back to the body hash (see _resolve_id).
        event_id=lambda body: body.get("eventId") or body.get("id"),
        event_type=lambda body: body.get("eventType") or body.get("type") or "notification",
        data=lambda body: body,
        secret_var="LINKEDIN_WEBHOOK_SECRET",
        relay_to=None,
    ),
    "openrouter": Provider(
        name="openrouter",
        domain="ai",
        signature_header="x-openrouter-signature",
        verify=signatures.verify_openrouter,
        event_id=lambda body: body.get("id") or body.get("event_id"),
        event_type=lambda body: body.get("type") or body.get("event") or "notification",
        data=lambda body: body.get("data") or body,
        secret_var="OPENROUTER_WEBHOOK_SECRET",
        relay_to=None,
    ),
}


def _resolve_id(provider: Provider, body: dict, payload: bytes) -> str:
    """The dedup identity. A provider that sends no event id still gets exact
    replay protection: the SHA-256 of the verified bytes IS a stable id for
    "this exact event", which is what dedup needs."""
    declared = provider.event_id(body)
    if declared:
        return str(declared)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


async def _claim(provider: str, event_id: str) -> bool:
    """Redis SETNX + 24h TTL (spec). True if WE claimed it — i.e. this is the
    first delivery. Raises on a Redis failure so the caller fails closed."""
    key = DEDUP_KEY.format(provider=provider, event_id=event_id)
    claimed = await store.get_redis().set(
        key, datetime.now(timezone.utc).isoformat(), nx=True, ex=DEDUP_TTL_SECONDS
    )
    return bool(claimed)


async def _release(provider: str, event_id: str) -> None:
    """Undo a claim so the provider's retry is not swallowed as a duplicate."""
    try:
        await store.get_redis().delete(DEDUP_KEY.format(provider=provider, event_id=event_id))
    except (redis.exceptions.RedisError, OSError):
        logger.exception("could not release dedup key for %s %s", provider, event_id)


def _normalize(provider: Provider, event_id: str, body: dict, request: Request) -> tuple[str, dict]:
    """(routing_key, payload) for the broker. One envelope shape across all
    providers so consumers do not branch on origin to find the basics."""
    event_type = provider.event_type(body)
    routing_key = f"{provider.domain}.{_flatten(event_type)}"
    payload = {
        "provider": provider.name,
        "provider_event_id": event_id,
        "provider_event_type": event_type,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "request_id": getattr(request.state, "request_id", None),
        "data": provider.data(body),
    }
    return routing_key, payload


async def _relay(provider: Provider, request: Request, payload: bytes) -> Response:
    """Forward the ORIGINAL bytes and headers (signature included) to the
    owning service, and answer the provider with that service's verdict — so
    a downstream failure still produces the non-2xx that makes the provider
    retry."""
    service, path = provider.relay_to
    client = proxy.get_client()
    headers = proxy.upstream_headers(request)
    try:
        upstream = await client.post(
            proxy.SERVICES[service] + path,
            content=payload,
            headers=headers,
            timeout=httpx.Timeout(RELAY_TIMEOUT_SECONDS),
        )
    except httpx.TimeoutException:
        return JSONResponse({"detail": f"Upstream timeout: {service}"}, status_code=504)
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"detail": f"Upstream unavailable: {service} ({exc.__class__.__name__})"},
            status_code=502,
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


async def _receive(provider: Provider, request: Request) -> Response:
    payload = await request.body()

    # ---- 1. verify BEFORE acting -----------------------------------------
    try:
        provider.verify(payload, request.headers.get(provider.signature_header, ""), provider.secret)
    except signatures.SignatureError as exc:
        logger.warning("%s webhook rejected: %s", provider.name, exc)
        return JSONResponse({"detail": f"Invalid {provider.name} webhook signature"}, status_code=401)

    try:
        body = json.loads(payload)
        if not isinstance(body, dict):
            raise ValueError("webhook body must be a JSON object")
    except ValueError:
        return JSONResponse({"detail": f"Malformed {provider.name} webhook payload"}, status_code=400)

    event_id = _resolve_id(provider, body, payload)

    # ---- 2. dedup (fails CLOSED: no dedup, no processing) -----------------
    try:
        claimed = await _claim(provider.name, event_id)
    except (redis.exceptions.RedisError, OSError):
        logger.exception("dedup store unavailable; refusing %s %s", provider.name, event_id)
        return JSONResponse(
            {"detail": "Webhook deduplication store unavailable; retry"}, status_code=503
        )
    if not claimed:
        logger.info("%s webhook %s already processed; acknowledged as duplicate", provider.name, event_id)
        return JSONResponse({"status": "duplicate", "provider": provider.name, "event_id": event_id})

    # ---- 3. publish the normalized event ---------------------------------
    routing_key, normalized = _normalize(provider, event_id, body, request)
    # pika is blocking; keep it off the event loop that is carrying live SSE.
    published = await run_in_threadpool(events.publish, routing_key, normalized, event_id)
    if published is None:
        logger.warning("broker unreachable; %s %s not announced", provider.name, event_id)

    # ---- 4. relay (Stripe) ------------------------------------------------
    if provider.relay_to is None:
        return JSONResponse({
            "status": "processed",
            "provider": provider.name,
            "event_id": event_id,
            "routing_key": routing_key,
            "published": published is not None,
        })

    relayed = await _relay(provider, request, payload)
    if relayed.status_code >= 500:
        # Retryable: let the provider's redelivery through instead of meeting
        # a dedup key that promises work we never finished.
        await _release(provider.name, event_id)
    return relayed


@router.post("/stripe")
async def stripe_webhook(request: Request) -> Response:
    return await _receive(PROVIDERS["stripe"], request)


@router.post("/linkedin")
async def linkedin_webhook(request: Request) -> Response:
    return await _receive(PROVIDERS["linkedin"], request)


@router.post("/openrouter")
async def openrouter_webhook(request: Request) -> Response:
    return await _receive(PROVIDERS["openrouter"], request)
