"""Request bodies for the Scraper service (responses are plain dicts, same
convention as the other services). The job-status/recurrence vocabulary
lives in store.py — single source of truth for routes, tasks, and the
consumer."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

import store


class ScrapeJobCreate(BaseModel):
    # Length cap mirrors url_guard.MAX_URL_LENGTH; the real SSRF gate runs in
    # the route (and consumer) via url_guard.validate_url.
    url: str = Field(min_length=1, max_length=2000)
    job_type: str = Field(default="page", min_length=1, max_length=50)
    # None = one-off; hourly/daily/weekly = the spec's recurring scrape
    # (re-armed by the beat scan after every run).
    recurrence: str | None = None

    @field_validator("recurrence")
    @classmethod
    def _known_recurrence(cls, v: str | None) -> str | None:
        if v is not None and v not in store.RECURRENCES:
            raise ValueError(f"recurrence must be one of {store.RECURRENCES}")
        return v
