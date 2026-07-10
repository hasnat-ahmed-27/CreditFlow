"""
Engine/session bootstrap for the auth service.

The engine is created lazily in `init_db()` (called from the FastAPI lifespan)
rather than at import time, so the Docker build-time smoke test
(`python -c "import main"`) doesn't need a live Postgres.

Schema strategy: `make_engine(schema="auth")` gives this service its own
Postgres schema, isolated from other services in the shared instance. Tables
are created with `Base.metadata.create_all` on startup (spec allows this
instead of Alembic — kept simple on purpose).
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from creditflow_common import config
from creditflow_common.db import Base, make_engine, make_session_factory

import models  # noqa: F401 — registers the auth tables on Base.metadata

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
        engine = make_engine(schema="auth")
    Base.metadata.create_all(engine)
    SessionLocal = make_session_factory(engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
