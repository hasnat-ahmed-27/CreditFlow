"""
Subject/body rendering per event type — plain-text, data-driven str.format
templates (no templating library: the spec's emails are short transactional
notices, and a missing payload field must degrade to an empty string, not a
crash that poisons the queue).

The spec's notification list is the source of truth for which templates
exist (Service 12: verification, receipt / payment-failed, invite,
post status / usage alerts — plus the low-balance alert Service 5 assigns to
us and the dunning downgrade notice from Billing's grace-period expiry):

  routing_key                     template                    recipient from
  user.registered              -> verification                payload email
  user.password_reset_requested-> password_reset              payload email
  invite.created               -> invite                      payload email
  member.joined                -> member_joined               payload email
  invoice.paid                 -> receipt                     account owner
  payment.failed               -> payment_failed              account owner
  subscription.downgraded      -> subscription_downgraded     account owner
  usage.threshold_reached      -> quota_threshold_{80|100}    account owner
  credits.low_balance          -> credits_low_balance         account owner
  post.published               -> post_published              account owner
  post.failed                  -> post_failed                 account owner

render() returns None for every other key that lands in our queues
(user.logged_in, account.*, subscription.updated, refund.issued,
ai.generation_failed, ...) — the consumer records the event as processed and
sends nothing.

Links point at the FRONTEND (NOTIFY_APP_BASE_URL) — the same convention as
Social's LINKEDIN_REDIRECT_URI: emails land in a browser, and the frontend
relays tokens to the right API call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Recipient sources — who the email goes to.
FROM_PAYLOAD = "payload"   # the event carries the address (email field)
FROM_ACCOUNT = "account"   # resolve via the recipients read model


def app_base_url() -> str:
    return os.getenv("NOTIFY_APP_BASE_URL", "http://localhost:5173").rstrip("/")


class _SafeDict(dict):
    """format_map helper: a field missing from the payload renders as ''
    instead of raising — a producer omitting an optional field must never
    dead-letter the event."""

    def __missing__(self, key: str) -> str:
        return ""


@dataclass(frozen=True)
class Rendered:
    template: str
    recipient_source: str  # FROM_PAYLOAD | FROM_ACCOUNT
    subject: str
    body: str


@dataclass(frozen=True)
class _Spec:
    template: str
    recipient_source: str
    subject: str
    body: str


_TEMPLATES: dict[str, _Spec] = {
    "user.registered": _Spec(
        template="verification",
        recipient_source=FROM_PAYLOAD,
        subject="Verify your CreditFlow email",
        body=(
            "Welcome to CreditFlow!\n\n"
            "Confirm your email address by opening this link:\n\n"
            "  {app_base_url}/verify-email?token={verification_token}\n\n"
            "If you did not sign up, you can ignore this email.\n"
        ),
    ),
    "user.password_reset_requested": _Spec(
        template="password_reset",
        recipient_source=FROM_PAYLOAD,
        subject="Reset your CreditFlow password",
        body=(
            "A password reset was requested for your CreditFlow account.\n\n"
            "Choose a new password here:\n\n"
            "  {app_base_url}/reset-password?token={reset_token}\n\n"
            "If you did not request this, you can ignore this email.\n"
        ),
    ),
    "invite.created": _Spec(
        template="invite",
        recipient_source=FROM_PAYLOAD,
        subject="You've been invited to {account_name} on CreditFlow",
        body=(
            "You have been invited to join {account_name} as {role}.\n\n"
            "Accept the invite here (valid until {expires_at}):\n\n"
            "  {app_base_url}/invites/accept?token={invite_token}\n"
        ),
    ),
    "member.joined": _Spec(
        template="member_joined",
        recipient_source=FROM_PAYLOAD,
        subject="Welcome to {account_name} on CreditFlow",
        body=(
            "You joined {account_name} as {role}.\n\n"
            "Sign in at {app_base_url} to get started.\n"
        ),
    ),
    "invoice.paid": _Spec(
        template="receipt",
        recipient_source=FROM_ACCOUNT,
        subject="Your CreditFlow payment receipt",
        body=(
            "We received your payment of {amount_display} {currency_display} "
            "for the {plan} plan.\n\n"
            "Invoice: {stripe_invoice_id}\n\n"
            "Thank you for using CreditFlow.\n"
        ),
    ),
    "payment.failed": _Spec(
        template="payment_failed",
        recipient_source=FROM_ACCOUNT,
        subject="Payment failed for your CreditFlow subscription",
        body=(
            "We could not collect {amount_display} {currency_display} for the "
            "{plan} plan.\n\n"
            "Your subscription is in a grace period until {grace_expires_at}. "
            "Please update your payment method at "
            "{app_base_url}/billing to keep your plan.\n"
        ),
    ),
    "subscription.downgraded": _Spec(
        template="subscription_downgraded",
        recipient_source=FROM_ACCOUNT,
        subject="Your CreditFlow subscription was downgraded",
        body=(
            "Your account was moved from the {previous_plan} plan to the "
            "{plan} plan (reason: {reason}).\n\n"
            "You can resubscribe any time at {app_base_url}/billing.\n"
        ),
    ),
    "credits.low_balance": _Spec(
        template="credits_low_balance",
        recipient_source=FROM_ACCOUNT,
        subject="Your CreditFlow credit balance is low",
        body=(
            "Your credit balance is down to {balance} credits (alert "
            "threshold: {threshold}).\n\n"
            "Top up at {app_base_url}/credits to keep generating.\n"
        ),
    ),
    "post.published": _Spec(
        template="post_published",
        recipient_source=FROM_ACCOUNT,
        subject="Your LinkedIn post is live",
        body=(
            "Your scheduled content ({content_id}) was published to "
            "LinkedIn.\n\n"
            "  {linkedin_post_url}\n"
        ),
    ),
    "post.failed": _Spec(
        template="post_failed",
        recipient_source=FROM_ACCOUNT,
        subject="Your LinkedIn post failed to publish",
        body=(
            "Your content ({content_id}) could not be published to "
            "LinkedIn.\n\n"
            "Reason: {error}\n\n"
            "Review it at {app_base_url}/content/{content_id}.\n"
        ),
    ),
}


def _money(cents) -> str:
    """Billing payloads carry integer cents (amount_paid / amount_due)."""
    try:
        return f"{int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return ""


def render(routing_key: str, data: dict) -> Rendered | None:
    """Rendered subject/body for a notifiable event, or None when this key
    does not produce an email (the consumer just records it as processed)."""
    if routing_key == "usage.threshold_reached":
        return _render_quota_threshold(data)

    spec = _TEMPLATES.get(routing_key)
    if spec is None:
        return None

    fields = _SafeDict(data)
    fields["app_base_url"] = app_base_url()
    fields["amount_display"] = _money(data.get("amount_paid", data.get("amount_due")))
    fields["currency_display"] = str(data.get("currency", "") or "").upper()
    return Rendered(
        template=spec.template,
        recipient_source=spec.recipient_source,
        subject=spec.subject.format_map(fields),
        body=spec.body.format_map(fields),
    )


def _render_quota_threshold(data: dict) -> Rendered:
    """80%/100% quota alerts share one body; the template name and subject
    carry the crossed threshold (Usage emits one event per crossing)."""
    try:
        pct = int(data.get("threshold_percent", 0))
    except (TypeError, ValueError):
        pct = 0
    if pct >= 100:
        subject = "You've reached your monthly AI quota"
    else:
        subject = f"You've used {pct}% of your monthly AI quota"
    fields = _SafeDict(data)
    fields["app_base_url"] = app_base_url()
    body = (
        "Your account has used {used_tokens} of {quota_tokens} tokens for "
        "the period {period}.\n\n"
        "Manage your plan at {app_base_url}/billing.\n"
    ).format_map(fields)
    return Rendered(
        template=f"quota_threshold_{pct}",
        recipient_source=FROM_ACCOUNT,
        subject=subject,
        body=body,
    )
