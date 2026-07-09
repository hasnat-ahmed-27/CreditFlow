"""
db.py — SQLAlchemy 2.0 engine/session factory shared by services.

Each service owns its own schema (search_path) in the single Postgres instance.
Pass schema=<service-name> so tables are isolated per service.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


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


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def session_dependency(SessionLocal: sessionmaker[Session]) -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
