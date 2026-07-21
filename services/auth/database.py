"""
Engine/session bootstrap for the auth service.

The engine is created lazily in `init_db()` (called from the FastAPI lifespan)
rather than at import time, so the Docker build-time smoke test
(`python -c "import main"`) doesn't need a live Postgres.

Schema strategy: `make_engine(schema="auth")` gives this service its own
Postgres schema, isolated from other services in the shared instance. Tables
are created with `Base.metadata.create_all` on startup (spec allows this
instead of Alembic — kept simple on purpose).

ADDED_COLUMNS is the companion to that choice: create_all never ALTERs a
table it already sees, so a column added to a shipped model would exist only
on fresh databases. Every column added after this service's tables first
shipped is listed here and topped up on startup — see
creditflow_common.db.add_missing_columns.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from creditflow_common import config
from creditflow_common.db import Base, add_missing_columns, make_engine, make_session_factory

import models  # noqa: F401 — registers the auth tables on Base.metadata

engine = None
SessionLocal = None

# table -> {column: DDL}. Both are nullable-or-defaulted, as they must be:
# existing rows need a value. Defaults match the model defaults.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "users": {"is_superadmin": "BOOLEAN NOT NULL DEFAULT FALSE"},
    "refresh_tokens": {"role": "VARCHAR(16) NOT NULL DEFAULT 'owner'"},
}


def init_db() -> None:
    global engine, SessionLocal
    if engine is not None:
        return
    schema = None
    if config.DATABASE_URL.startswith("sqlite"):
        # Test-only path: file-based SQLite, shared across TestClient threads.
        engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})
    else:
        schema = "auth"
        engine = make_engine(schema=schema)
    Base.metadata.create_all(engine)
    for table, columns in ADDED_COLUMNS.items():
        add_missing_columns(engine, table, columns, schema=schema)
    SessionLocal = make_session_factory(engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
