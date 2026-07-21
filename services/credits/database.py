"""
Engine/session bootstrap for the credits/marketplace service.

Same lazy pattern as the other services: the engine is created in `init_db()`
(FastAPI lifespan), not at import time, so the Docker build-time smoke test
(`python -c "import main"`) doesn't need a live Postgres.

Schema strategy: `make_engine(schema="credits")` isolates this service's
tables in its own Postgres schema per the spec's schema-per-service rule.

ADDED_COLUMNS covers what create_all cannot: it never ALTERs a table it
already sees, so a column added to a shipped model would exist only on fresh
databases. See creditflow_common.db.add_missing_columns.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from creditflow_common import config
from creditflow_common.db import Base, add_missing_columns, make_engine, make_session_factory

import models  # noqa: F401 — registers credits_ledger/marketplace_listings/processed_events on Base.metadata

engine = None
SessionLocal = None

# The AI-generation debit's business key (models.py). Nullable — every row
# written before it existed predates generation debits. The index matters:
# it is looked up on every ai.generation_completed to prevent a double-debit,
# and a model's index=True is only honoured when create_all builds the table.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "credits_ledger": {"job_id": "VARCHAR(36)"},
}
ADDED_INDEXES: dict[str, dict[str, str]] = {
    "credits_ledger": {"ix_credits_ledger_job_id": "job_id"},
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
        schema = "credits"
        engine = make_engine(schema=schema)
    Base.metadata.create_all(engine)
    for table, columns in ADDED_COLUMNS.items():
        add_missing_columns(engine, table, columns,
                            indexes=ADDED_INDEXES.get(table), schema=schema)
    SessionLocal = make_session_factory(engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
