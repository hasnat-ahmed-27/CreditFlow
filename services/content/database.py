"""
Engine/session bootstrap for the Content service.

Same lazy pattern as the other services: the engine is created in `init_db()`
(FastAPI lifespan), not at import time, so the Docker build-time smoke test
(`python -c "import main"`) doesn't need a live Postgres.

Schema strategy: `make_engine(schema="content")` isolates this service's
tables in its own Postgres schema per the spec's schema-per-service rule.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from creditflow_common import config
from creditflow_common.db import Base, make_engine, make_session_factory

# idempotency must be imported HERE (not only in consumer.py, which the
# lifespan imports AFTER init_db's create_all) or a fresh database never gets
# the processed_events table and every consumed event dead-letters.
import creditflow_common.idempotency  # noqa: F401 — registers processed_events
import models  # noqa: F401 — registers content/content_versions on Base.metadata

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
        engine = make_engine(schema="content")
    Base.metadata.create_all(engine)
    SessionLocal = make_session_factory(engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
