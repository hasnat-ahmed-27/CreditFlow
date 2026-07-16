"""Request payloads. Responses are plain dicts, documented in routes.py."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Checkout only sells paid tiers; "free" is reached via plan change/dunning.
PaidPlan = Literal["pro", "team"]
Plan = Literal["free", "pro", "team"]


class CheckoutRequest(BaseModel):
    plan: PaidPlan


class ChangePlanRequest(BaseModel):
    plan: Plan


class RefundRequest(BaseModel):
    # Either our invoice id (looked up for the payment intent) or a Stripe
    # payment intent directly.
    invoice_id: str | None = None
    stripe_payment_intent_id: str | None = None
    amount: int | None = Field(default=None, gt=0, description="cents; omit for a full refund")
    reason: Literal["duplicate", "fraudulent", "requested_by_customer"] | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if bool(self.invoice_id) == bool(self.stripe_payment_intent_id):
            raise ValueError("provide exactly one of invoice_id or stripe_payment_intent_id")
        return self
