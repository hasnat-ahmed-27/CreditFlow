"""
Engine/session bootstrap for the Admin service.

Same lazy pattern as the other services: the engine is created in `init_db()`
(FastAPI lifespan), not at import time, so the Docker build-time smoke test
(`python -c "import main"`) doesn't need a live Postgres.

Schema strategy: `make_engine(schema="admin")` isolates this service's
tables in its own Postgres schema per the spec's schema-per-service rule.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from creditflow_common import config
from creditflow_common.db import Base, make_engine, make_session_factory

# noqa comments: imported for their side effect of registering tables on
# Base.metadata BEFORE init_db's create_all — idempotency in particular must
# be here (not just in consumer.py, which the lifespan imports after init_db)
# or a fresh database never gets the processed_events table.
import creditflow_common.idempotency  # noqa: F401 — registers processed_events
import models  # noqa: F401 — registers audit_log/accounts_directory/users_directory

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
        engine = make_engine(schema="admin")
    Base.metadata.create_all(engine)
    SessionLocal = make_session_factory(engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
