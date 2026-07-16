"""
Test bootstrap. Must run BEFORE any app import: creditflow_common.config and
celery_app read the environment at import time, so we point them at
throwaway/test resources here — SQLite instead of Postgres for the
processed_events ledger, an EPHEMERAL RS256 keypair (the real private key is
gitignored, so CI never has it; tests sign their own tokens with the
throwaway key and the app verifies with its public half), a stub publisher
instead of RabbitMQ, and mongomock instead of MongoDB (store.get_client is
monkeypatched — the MONGO_URL ALSO points at a dead address as a
belt-and-braces guard, so an unmocked call fails instantly instead of
touching the network). The scraping engine is fully monkeypatched:
scraper_engine.scrape — the one seam the worker calls — returns canned
extracted content, so no test ever launches Playwright or fetches a page
(engine unit tests exercise extract()/robots_allowed() directly, with the
robots fetch faked). Celery runs EAGER (task_always_eager +
task_eager_propagates via SCRAPER_CELERY_EAGER=1): every .delay() executes
inline and synchronously, so no worker, no beat, and no broker connection
ever happen — the broker URL also points at a dead address. Beat itself
never runs in tests; the due-scan is exercised by calling
tasks.scan_due_scrapes directly (the exact function beat would tick). The
consumer thread is disabled — tests call consumer.handle_event directly,
exercising the exact function the broker would. This is why the suite runs
in CI with no infra containers, no browser, and no secrets.
"""
from __future__ import annotations

import os
import tempfile
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_TMP = tempfile.mkdtemp(prefix="creditflow_scraper_test_")
_DB_FILE = os.path.join(_TMP, f"scraper_{uuid.uuid4().hex}.db")
_PRIV_PEM = os.path.join(_TMP, "jwt_private.pem")
_PUB_PEM = os.path.join(_TMP, "jwt_public.pem")

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
with open(_PRIV_PEM, "wb") as f:
    f.write(_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
with open(_PUB_PEM, "wb") as f:
    f.write(_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))

os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE}"
os.environ["JWT_PRIVATE_KEY_PATH"] = _PRIV_PEM
os.environ["JWT_PUBLIC_KEY_PATH"] = _PUB_PEM
os.environ["SCRAPER_CONSUMER_ENABLED"] = "0"        # no broker in tests
os.environ["SCRAPER_CELERY_EAGER"] = "1"            # tasks run inline, no worker/beat
os.environ["CELERY_BROKER_URL"] = "redis://127.0.0.1:9/0"      # guard: never contacted (eager)
os.environ["CELERY_RESULT_BACKEND"] = "redis://127.0.0.1:9/0"  # guard: never contacted (eager)
# guard: store.get_client is monkeypatched to mongomock; if anything slips
# past it, this dead address fails in milliseconds instead of hanging.
os.environ["MONGO_URL"] = ("mongodb://127.0.0.1:9/"
                           "?serverSelectionTimeoutMS=100&connectTimeoutMS=100")
os.environ["SCRAPER_MIN_DELAY_SECONDS"] = "0"       # rate limiter never sleeps in tests

import mongomock  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
import events  # noqa: E402
import main  # noqa: E402
import scraper_engine  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def fake_mongo(monkeypatch):
    """Fresh in-memory MongoDB per test; store.get_client is the single
    accessor every store.py helper (and therefore every route/task/consumer
    path) goes through. Returns the client so tests can inspect collections
    directly via store.get_db()."""
    client = mongomock.MongoClient()
    monkeypatch.setattr(store, "get_client", lambda: client)
    store.init()  # exercise index creation against the fake
    return client


@pytest.fixture(autouse=True)
def published_events(monkeypatch):
    """Capture RabbitMQ events instead of talking to a broker; returns
    [(routing_key, payload)]. Tasks and the consumer both publish through
    the same events.publish."""
    captured: list[tuple[str, dict]] = []

    def _fake_publish(routing_key: str, payload: dict) -> str:
        captured.append((routing_key, payload))
        return "test-event-id"

    monkeypatch.setattr(events, "publish", _fake_publish)
    return captured


# What the fake engine hands back by default; tests read these constants
# instead of retyping magic strings.
CANNED_TITLE = "Competitor pricing page"
CANNED_DESCRIPTION = "Plans and pricing for the competitor product."
CANNED_TEXT = "Pro plan $49/month. Team plan $199/month. Contact sales for enterprise."
CANNED_HEADINGS = ["Pricing", "Pro", "Team"]
CANNED_LINKS = ["https://competitor.example/signup", "https://competitor.example/docs"]
CANNED_HTML = "<html><head><title>Competitor pricing page</title></head><body>...</body></html>"


@pytest.fixture(autouse=True)
def scrape_engine(monkeypatch):
    """Replace scraper_engine.scrape — the ONE seam the worker calls — with a
    recording fake. state["calls"] logs (url, job_type); tests inject
    failures by setting state["error"] to an exception instance, or change
    the canned content via state["result"]."""
    state = {
        "calls": [],
        "error": None,  # exception to raise instead of returning
        "result": {
            "title": CANNED_TITLE,
            "description": CANNED_DESCRIPTION,
            "headings": list(CANNED_HEADINGS),
            "text": CANNED_TEXT,
            "links": list(CANNED_LINKS),
            "html": CANNED_HTML,
            "status_code": 200,
        },
    }

    def _fake_scrape(url: str, job_type: str = "page") -> dict:
        state["calls"].append({"url": url, "job_type": job_type})
        if state["error"] is not None:
            raise state["error"]
        return {**state["result"], "final_url": url}

    monkeypatch.setattr(scraper_engine, "scrape", _fake_scrape)
    return state


@pytest.fixture()
def client(fake_mongo):
    # Context manager triggers the lifespan (DB init + Mongo indexes) like a
    # real startup; depending on fake_mongo guarantees the lifespan's
    # store.init() hits mongomock, never a real client. Each test starts from
    # an empty Postgres ledger — processed_events is service-global, so
    # leftover rows from a previous test would poison assertions (the Mongo
    # side is naturally fresh: a new mongomock client per test).
    with TestClient(main.app) as c:
        from creditflow_common.db import Base
        with database.engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
        yield c


@pytest.fixture()
def db_session(client):
    """Direct Postgres-ledger access for test setup/inspection. Depends on
    `client` so init_db has run."""
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()
