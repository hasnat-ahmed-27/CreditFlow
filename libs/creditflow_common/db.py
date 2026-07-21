"""
db.py — SQLAlchemy 2.0 engine/session factory shared by services.

Each service owns its own schema (search_path) in the single Postgres instance.
Pass schema=<service-name> so tables are isolated per service.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config

logger = logging.getLogger("creditflow.db")


class Base(DeclarativeBase):
    pass


def make_engine(schema: str | None = None):
    engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, pool_size=5)
    if schema:
        # Ensure the per-service schema exists and is on the search path.
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        engine = engine.execution_options(schema_translate_map={None: schema})
    return engine


def add_missing_columns(
    engine,
    table: str,
    columns: dict[str, str],
    indexes: dict[str, str] | None = None,
    schema: str | None = None,
) -> list[str]:
    """Additive, idempotent schema top-up for a table that ALREADY EXISTS.

    WHY THIS EXISTS: services bootstrap with `Base.metadata.create_all`, which
    the spec sanctions instead of Alembic ("kept simple on purpose"). But
    create_all only ever creates MISSING TABLES — it will never ALTER an
    existing one. So adding a column to a shipped model is invisible to it:
    fresh databases (every test run, every clean `docker compose up`) get the
    column, while a deployment with an existing volume keeps the old table and
    every query naming the new column fails with UndefinedColumn. Unit tests
    structurally cannot catch this, because they always start from an empty
    database.

    This closes that gap without adopting a migration framework: each service
    declares the columns it has added since its table first shipped, and this
    adds any that are absent. It is deliberately narrow — ADD COLUMN only,
    never a drop, retype, or rename, so it cannot destroy data. Anything
    beyond additive change is the point at which this project should adopt
    Alembic rather than extend this helper.

    Columns must be nullable or carry a DEFAULT (an existing row has to get
    SOME value). `indexes` covers the fact that a model's index=True is also
    only honoured when create_all builds the table.

    Returns the column names actually added (empty when already up to date).
    """
    inspector = inspect(engine)
    if not inspector.has_table(table, schema=schema):
        return []  # create_all will build it complete, indexes and all

    qualified = f'"{schema}"."{table}"' if schema else f'"{table}"'
    existing = {c["name"] for c in inspector.get_columns(table, schema=schema)}
    added = []
    with engine.begin() as conn:
        for name, ddl in columns.items():
            if name in existing:
                continue
            conn.execute(text(f'ALTER TABLE {qualified} ADD COLUMN "{name}" {ddl}'))
            added.append(name)
            logger.warning("added missing column %s.%s (%s)", table, name, ddl)
        for index_name, column in (indexes or {}).items():
            conn.execute(text(
                f'CREATE INDEX IF NOT EXISTS "{index_name}" ON {qualified} ("{column}")'
            ))
    return added


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def session_dependency(SessionLocal: sessionmaker[Session]) -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
