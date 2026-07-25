"""
Test bootstrap. Must run BEFORE the app import: proxy.py, ratelimit.py,
webhooks.py, and creditflow_common.config all read the environment at import
time, so everything is pinned to throwaway/test resources HERE.

Belt-and-braces, no-infra, no-secrets posture (same as every other service's
conftest):
  - Upstream URLs -> a dead address (http://127.0.0.1:9, discard port: connects
    fail instantly). The autouse `upstream` fixture then swaps the proxy's
    httpx client for a MockTransport, so no test opens a real socket.
  - An EPHEMERAL RS256 keypair written to a temp dir (the real private key is
    gitignored, so CI never has it). Tests SIGN their own access tokens with
    the throwaway private key; the gateway VERIFIES with its public half —
    exactly the split the platform runs in production. A SECOND keypair
    (`WRONG_PRIVATE_KEY`) signs the "invalid signature" fixtures.
  - fakeredis (async) instead of Redis, fresh per test, for the rate-limit
    counters and the webhook dedup keys.
  - A capturing stub for the RabbitMQ publisher, so webhook tests assert what
    WOULD be published without a broker.

This is why the suite runs in CI with no infra containers and no secrets.
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# --- dead upstreams -------------------------------------------------------
DEAD = "http://127.0.0.1:9"
UPSTREAM_ENV_VARS = (
    "AUTH_URL", "USER_URL", "BILLING_URL", "CREDITS_URL", "USAGE_URL",
    "AI_URL", "CONTENT_URL", "SCHEDULER_URL", "SOCIAL_URL", "SCRAPER_URL",
    "NOTIFICATION_URL", "ADMIN_URL",
)
for _var in UPSTREAM_ENV_VARS:
    os.environ[_var] = DEAD

# --- ephemeral RS256 keypair(s) -------------------------------------------
_TMP = tempfile.mkdtemp(prefix="creditflow_gateway_test_")
_PRIV_PEM = os.path.join(_TMP, "jwt_private.pem")
_PUB_PEM = os.path.join(_TMP, "jwt_public.pem")


def _write_keypair(priv_path: str, pub_path: str) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with open(priv_path, "wb") as f:
        f.write(priv_pem)
    with open(pub_path, "wb") as f:
        f.write(pub_pem)
    return priv_pem.decode("utf-8")


PRIVATE_KEY = _write_keypair(_PRIV_PEM, _PUB_PEM)
# A key the gateway does NOT trust — used to forge "invalid signature" tokens.
_WRONG_PRIV = os.path.join(_TMP, "wrong_private.pem")
_WRONG_PUB = os.path.join(_TMP, "wrong_public.pem")
WRONG_PRIVATE_KEY = _write_keypair(_WRONG_PRIV, _WRONG_PUB)

os.environ["JWT_PRIVATE_KEY_PATH"] = _PRIV_PEM
os.environ["JWT_PUBLIC_KEY_PATH"] = _PUB_PEM
os.environ["JWT_ISSUER"] = "creditflow-auth"

# --- rate limits: effectively unlimited by default; the rate-limit tests
# dial the module globals down. ------------------------------------------
os.environ["GATEWAY_RATE_LIMIT_PER_MINUTE"] = "100000"
os.environ["GATEWAY_ACCOUNT_RATE_LIMIT_PER_MINUTE"] = "100000"

# --- webhook secrets: known test values so fixtures can sign valid ones ---
STRIPE_TEST_SECRET = "whsec_test_gateway"
LINKEDIN_TEST_SECRET = "linkedin_test_secret"
OPENROUTER_TEST_SECRET = "openrouter_test_secret"
os.environ["STRIPE_WEBHOOK_SECRET"] = STRIPE_TEST_SECRET
os.environ["LINKEDIN_WEBHOOK_SECRET"] = LINKEDIN_TEST_SECRET
os.environ["OPENROUTER_WEBHOOK_SECRET"] = OPENROUTER_TEST_SECRET

import fakeredis  # noqa: E402
import httpx  # noqa: E402
import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from creditflow_common import config  # noqa: E402

import events  # noqa: E402
import main  # noqa: E402
import proxy  # noqa: E402
import ratelimit  # noqa: E402, F401 — loaded under the test env for import-time config (see docstring)
import store  # noqa: E402


# ==========================================================================
# Token minting — the test side of the RS256 split
# ==========================================================================

def make_token(
    role: str = "member",
    account_id: str = "acc-test",
    user_id: str = "user-1",
    *,
    token_type: str = "access",
    ttl_seconds: int = 900,
    issuer: str | None = None,
    jti: str | None = None,
    key: str | None = None,
) -> str:
    """Sign an access token with the throwaway private key. Every knob a
    negative-path test needs is a parameter: an expired token (ttl<0), a
    refresh token (token_type), a foreign issuer, or a wrong signing key."""
    now = int(time.time())
    payload = {
        "sub": user_id,
        "account_id": account_id,
        "role": role,
        "type": token_type,
        "jti": jti or uuid.uuid4().hex,
        "iss": issuer or config.JWT_ISSUER,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return pyjwt.encode(payload, key or PRIVATE_KEY, algorithm="RS256")


def bearer(role: str = "member", **kwargs) -> dict[str, str]:
    """Authorization header for a freshly minted token."""
    return {"Authorization": f"Bearer {make_token(role=role, **kwargs)}"}


@pytest.fixture()
def auth() -> dict[str, str]:
    """A valid MEMBER bearer — the default identity for tests that just need
    to clear authentication."""
    return bearer("member")


@pytest.fixture()
def owner_auth() -> dict[str, str]:
    return bearer("owner")


# ==========================================================================
# MockTransport upstreams (unchanged machinery from the original conftest)
# ==========================================================================

class _LazyStream(httpx.AsyncByteStream):
    """Wrap an async iterable so the mock response behaves like a live socket:
    unconsumed until the gateway actually reads it (a Response built from
    plain content= is pre-read, and aiter_raw refuses it)."""

    def __init__(self, aiterable):
        self._aiterable = aiterable

    async def __aiter__(self):
        async for chunk in self._aiterable:
            yield chunk


async def _one_chunk(body: bytes):
    yield body


class Upstream:
    """Configurable fake upstream behind an httpx.MockTransport.

    `.requests` records every outgoing httpx.Request the gateway built;
    `.respond(...)` / `.raise_error(...)` set the canned behaviour (default:
    200 {"ok": true}). `.respond_for(path_substr, ...)` overrides a specific
    path so the webhook relay (POST /webhooks/stripe -> billing) and the
    aggregation fan-out can each get a distinct answer."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self._default = self._make_responder(json={"ok": True})
        self._overrides: list[tuple[str, callable]] = []

    def _make_responder(self, status_code=200, *, json=None, content=None,
                        headers=None, stream_factory=None):
        def _responder(request: httpx.Request) -> httpx.Response:
            if stream_factory is not None:
                return httpx.Response(
                    status_code, headers=headers, stream=_LazyStream(stream_factory())
                )
            kwargs = {}
            if headers is not None:
                kwargs["headers"] = headers
            if json is not None:
                kwargs["json"] = json
            elif content is not None:
                kwargs["content"] = content
            template = httpx.Response(status_code, **kwargs)
            return httpx.Response(
                status_code,
                headers=template.headers,
                stream=_LazyStream(_one_chunk(template.content)),
            )

        return _responder

    def respond(self, status_code=200, *, json=None, content=None, headers=None,
                stream_factory=None):
        self._default = self._make_responder(
            status_code, json=json, content=content, headers=headers,
            stream_factory=stream_factory,
        )

    def respond_for(self, path_substr: str, status_code=200, *, json=None,
                    content=None, headers=None):
        responder = self._make_responder(
            status_code, json=json, content=content, headers=headers
        )
        # Replace any prior override for this path so a test can flip the same
        # upstream's answer between calls (e.g. Billing 500 then 200 on retry).
        self._overrides = [(p, r) for p, r in self._overrides if p != path_substr]
        self._overrides.append((path_substr, responder))

    def raise_error(self, exc: Exception):
        def _responder(request: httpx.Request) -> httpx.Response:
            raise exc

        self._default = _responder

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for path_substr, responder in self._overrides:
            if path_substr in str(request.url):
                return responder(request)
        return self._default(request)


@pytest.fixture(autouse=True)
def upstream(monkeypatch):
    """Fresh fake upstream per test, installed as the proxy's client."""
    fake = Upstream()
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    monkeypatch.setattr(proxy, "get_client", lambda: mock_client)
    return fake


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Fresh in-memory async Redis per test — isolates rate-limit counters and
    webhook dedup keys."""
    monkeypatch.setattr(store, "_client", fakeredis.FakeAsyncRedis(decode_responses=True))


@pytest.fixture(autouse=True)
def published(monkeypatch):
    """Capture RabbitMQ publishes instead of talking to a broker; returns the
    list of (routing_key, payload, event_id) the gateway tried to publish.
    Returns a truthy event_id so the endpoint reports published=true."""
    captured: list[tuple[str, dict, str | None]] = []

    def _fake_publish(routing_key: str, payload: dict, event_id: str | None = None) -> str:
        captured.append((routing_key, payload, event_id))
        return event_id or "test-event-id"

    monkeypatch.setattr(events, "publish", _fake_publish)
    return captured


@pytest.fixture()
def compose_urls(monkeypatch):
    """The real compose topology (http://<name>:8000) — undialable from the
    test host and intercepted by the MockTransport anyway, but it lets the
    routing tests assert the exact upstream each prefix resolves to."""
    monkeypatch.setattr(
        proxy, "SERVICES", {name: f"http://{name}:8000" for name in proxy.SERVICES}
    )


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c
