"""
Engine/session bootstrap — for the IDEMPOTENCY LEDGER ONLY.

This service's domain data (scrape jobs + scraped documents) lives in
MongoDB per the spec ("Database Ownership: MongoDB — collection:
scraped_documents") — see store.py, the only module that touches Mongo. What
stays on the shared Postgres is the one cross-service invariant every
consumer in this repo shares: the processed_events dedup table
(creditflow_common.idempotency), which needs the transactional
insert-or-conflict semantics consumer.py leans on.

Same lazy pattern as the other services: the engine is created in `init_db()`
(FastAPI lifespan), not at import time, so the Docker build-time smoke test
(`python -c "import main"`) doesn't need a live Postgres.

Schema strategy: `make_engine(schema="scraper")` isolates this service's
table in its own Postgres schema per the spec's schema-per-service rule.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from creditflow_common import config
from creditflow_common.db import Base, make_engine, make_session_factory

# noqa comment: imported for its side effect of registering processed_events
# on Base.metadata BEFORE init_db's create_all. It must be HERE (not just in
# consumer.py, which the lifespan imports after init_db) or a fresh database
# never gets the table — the fix the Social service already applies.
import creditflow_common.idempotency  # noqa: F401 — registers processed_events

engine = None
SessionLocal = None


def init_db() -> None:
    global engine, SessionLocal
    if engine is not None:
        return
    if config.DATABASE_URL.startswith("sqlite"):
        # Test-only path: file-based SQLite, shared across TestClient threads.
        engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})
    else:
        engine = make_engine(schema="scraper")
    Base.metadata.create_all(engine)
    SessionLocal = make_session_factory(engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
