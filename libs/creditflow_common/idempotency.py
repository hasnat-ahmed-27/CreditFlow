"""
idempotency.py — exactly-once event processing via a processed_events table.

Every consumer, before applying an event, calls `already_processed(db, event_id)`.
If the event_id is new it inserts a row (unique constraint) and returns False;
if it was seen before it returns True and the consumer skips the work. This makes
consumers safe against RabbitMQ redelivery / at-least-once duplicates.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Base


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    event_id = Column(String(64), primary_key=True)
    processed_at = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))


def already_processed(db: Session, event_id: str) -> bool:
    """
    Returns True if this event_id was handled before. Otherwise records it and
    returns False. Relies on the primary-key/unique constraint to be race-safe.
    """
    existing = db.get(ProcessedEvent, event_id)
    if existing is not None:
        return True
    db.add(ProcessedEvent(event_id=event_id))
    try:
        db.flush()
        return False
    except IntegrityError:
        db.rollback()
        return True
