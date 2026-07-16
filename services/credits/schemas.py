"""Request bodies. Field-level validation only — balance checks and listing
state transitions live in the route transactions where they can be atomic."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ConsumeRequest(BaseModel):
    amount: int = Field(gt=0, description="Credits to debit (AI usage spend)")
    reason: str | None = Field(default=None, max_length=255)


class ListingCreate(BaseModel):
    credits_amount: int = Field(gt=0, description="Credits offered for sale")
    price_cents: int = Field(gt=0, description="Asking price in cents")
