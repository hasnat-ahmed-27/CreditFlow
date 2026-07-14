"""Request bodies for the Content service (responses are plain dicts, same
convention as the other services)."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# Single source of truth for the lifecycle vocabulary (transitions live in
# routes.TRANSITIONS).
STATUSES = ("draft", "approved", "scheduled", "published")


def _clean_tags(tags: list[str]) -> list[str]:
    cleaned = []
    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue
        if "," in tag:
            # Commas are the storage delimiter (see models docstring).
            raise ValueError("tags must not contain commas")
        if len(tag) > 50:
            raise ValueError("tags must be at most 50 characters")
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned


class ContentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=100_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    # External image reference; uploads go through POST /content/{id}/image.
    image_url: str | None = Field(default=None, max_length=1000)
    # Optional link back to the AI generation this draft came from.
    generation_job_id: str | None = Field(default=None, max_length=36)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str]) -> list[str]:
        return _clean_tags(v)


class ContentUpdate(BaseModel):
    """Partial update — only the provided fields change (a versioned edit)."""
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, min_length=1, max_length=100_000)
    tags: list[str] | None = Field(default=None, max_length=20)
    image_url: str | None = Field(default=None, max_length=1000)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_tags(v)


class StatusChange(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        return v
