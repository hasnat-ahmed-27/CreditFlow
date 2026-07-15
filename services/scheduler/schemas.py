"""Request bodies for the Scheduler service (responses are plain dicts, same
convention as the other services).

Datetime convention: `publish_at` may arrive timezone-aware (converted to UTC
as-is) or naive (interpreted in the request's `timezone` field — "9am" means
9am where the user lives). Storage is always UTC; the timezone string rides
along for recurrence math and calendar display (see models docstring).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from recurrence import RECURRENCES, validate_timezone

# Single source of truth for the schedule lifecycle vocabulary (transitions
# live in routes/tasks: pending -> fired | cancelled).
STATUSES = ("pending", "fired", "cancelled")


def _clean_recurrence(v: str | None) -> str | None:
    if v is not None and v not in RECURRENCES:
        raise ValueError(f"recurrence must be one of {RECURRENCES}")
    return v


class ScheduleCreate(BaseModel):
    content_id: str = Field(min_length=1, max_length=36)
    publish_at: datetime
    timezone: str = Field(default="UTC", max_length=64)
    # None = one-off; a cadence makes it recurring (bonus feature).
    recurrence: str | None = Field(default=None)

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, v: str) -> str:
        return validate_timezone(v)

    @field_validator("recurrence")
    @classmethod
    def check_recurrence(cls, v: str | None) -> str | None:
        return _clean_recurrence(v)


class ScheduleUpdate(BaseModel):
    """Reschedule — partial update; only provided fields change. An EXPLICIT
    `"recurrence": null` converts a recurring schedule to a one-off (routes
    distinguishes omitted-vs-null via exclude_unset)."""
    publish_at: datetime | None = Field(default=None)
    timezone: str | None = Field(default=None, max_length=64)
    recurrence: str | None = Field(default=None)

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, v: str | None) -> str | None:
        return None if v is None else validate_timezone(v)

    @field_validator("recurrence")
    @classmethod
    def check_recurrence(cls, v: str | None) -> str | None:
        return _clean_recurrence(v)
