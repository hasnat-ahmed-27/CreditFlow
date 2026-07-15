"""
Recurrence math for the "recurring schedules" bonus (spec: "allow a scheduled
post to repeat on a cadence (e.g. weekly)").

Cadences are the calendar presets the spec's example implies — daily, weekly,
monthly — not raw cron: the calendar UI schedules "every Monday 9am", and a
preset survives DST where a fixed 168h interval would drift.

The math is wall-clock-preserving: the current occurrence (UTC) is converted
to the schedule's IANA timezone, the cadence is added to the LOCAL time, and
the result is converted back to UTC. So a daily 9am America/New_York schedule
fires at 14:00 UTC in winter and 13:00 UTC in summer, staying 9am local.
Monthly clamps to the target month's last day (Jan 31 -> Feb 28/29).
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Single source of truth for the cadence vocabulary (schemas validates against it).
RECURRENCES = ("daily", "weekly", "monthly")


def validate_timezone(name: str) -> str:
    """Return the name if it is a known IANA zone, else raise ValueError."""
    try:
        ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 — ZoneInfoNotFoundError, ValueError, ...
        raise ValueError(f"unknown timezone: {name!r}") from exc
    return name


def next_occurrence(after_utc: datetime, recurrence: str, tz_name: str) -> datetime:
    """The occurrence following `after_utc` (aware, UTC), returned aware-UTC."""
    if recurrence not in RECURRENCES:
        raise ValueError(f"recurrence must be one of {RECURRENCES}")
    tz = ZoneInfo(tz_name)
    local = after_utc.astimezone(tz).replace(tzinfo=None)  # wall-clock time

    if recurrence == "daily":
        local = local + timedelta(days=1)
    elif recurrence == "weekly":
        local = local + timedelta(days=7)
    else:  # monthly — same day next month, clamped to the month's length
        year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        day = min(local.day, calendar.monthrange(year, month)[1])
        local = local.replace(year=year, month=month, day=day)

    return local.replace(tzinfo=tz).astimezone(timezone.utc)
