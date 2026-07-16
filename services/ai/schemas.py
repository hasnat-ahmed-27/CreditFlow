"""Request bodies for the AI generation service (responses are plain dicts,
same convention as the other services)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class GenerationRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=32_000)
    # Alias ("fast" / "quality") or a full OpenRouter id from the allowlist —
    # anything else is rejected, so the account can't be billed for arbitrary
    # models we never priced.
    model: str = Field(default="fast", max_length=128)
    max_tokens: int | None = Field(default=None, ge=1, le=16_000)
