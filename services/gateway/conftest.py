"""
Test bootstrap. Must run BEFORE the app import: proxy.py reads the upstream
URLs from the environment at import time, so every one is pointed at a dead
address here (http://127.0.0.1:9 — discard port, connects fail instantly) as
a belt-and-braces guard. The autouse `upstream` fixture then swaps the
proxy's httpx client for one backed by a MockTransport, so no test can ever
open a real socket. The gateway holds no DB, Redis, or broker state, so
nothing else needs faking — the suite runs with plain `pytest`, zero infra,
zero secrets.
"""
from __future__ import annotations

import os

DEAD = "http://127.0.0.1:9"
UPSTREAM_ENV_VARS = (
    "AUTH_URL", "USER_URL", "BILLING_URL", "CREDITS_URL", "USAGE_URL",
    "AI_URL", "CONTENT_URL", "SCHEDULER_URL", "SOCIAL_URL", "SCRAPER_URL",
    "NOTIFICATION_URL", "ADMIN_URL",
)
for _var in UPSTREAM_ENV_VARS:
    os.environ[_var] = DEAD
# Effectively unlimited; the rate-limit tests dial the module global down.
os.environ["GATEWAY_RATE_LIMIT_PER_MINUTE"] = "100000"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import proxy  # noqa: E402


class _LazyStream(httpx.AsyncByteStream):
    """Wrap an async iterable so the mock response behaves like a live
    socket: unconsumed until the gateway actually reads it (a Response built
    from plain content= is pre-read, and aiter_raw refuses it)."""

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
    200 {"ok": true}).
    """

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.respond(json={"ok": True})

    def respond(self, status_code=200, *, json=None, content=None, headers=None,
                stream_factory=None):
        def _responder(request: httpx.Request) -> httpx.Response:
            if stream_factory is not None:
                return httpx.Response(
                    status_code, headers=headers, stream=_LazyStream(stream_factory())
                )
            # Build once to get body + content-type/length, then re-serve it
            # as a lazy stream.
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

        self._responder = _responder

    def raise_error(self, exc: Exception):
        def _responder(request: httpx.Request) -> httpx.Response:
            raise exc

        self._responder = _responder

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


@pytest.fixture(autouse=True)
def upstream(monkeypatch):
    """Fresh fake upstream per test, installed as the proxy's client."""
    fake = Upstream()
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    monkeypatch.setattr(proxy, "get_client", lambda: mock_client)
    return fake


@pytest.fixture(autouse=True)
def clean_rate_buckets():
    main._rate_buckets.clear()
    yield
    main._rate_buckets.clear()


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
