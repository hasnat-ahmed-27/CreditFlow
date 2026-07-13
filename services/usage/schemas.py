"""Request bodies for the usage service (responses are plain dicts, same
convention as the other services)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class QuotaCheckRequest(BaseModel):
    # How many tokens the caller is about to spend. The AI service rarely
    # knows this before streaming, so it defaults to 1 — "is there any
    # headroom left at all", the cheapest useful pre-check.
    estimated_tokens: int = Field(default=1, ge=0)
