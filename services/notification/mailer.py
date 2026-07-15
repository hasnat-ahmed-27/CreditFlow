"""
Email provider layer — the ONLY module that performs an HTTP call to Resend
or Mailgun (spec: "Use a free-tier transactional email provider (Resend or
Mailgun sandbox) — no self-hosted SMTP"). Same single-seam rule as Social's
linkedin.py: tests mock send_via_resend / send_via_mailgun here and nothing
else in the service can reach the network.

Provider order: Resend is primary; Mailgun is the fallback when configured.
A provider is "configured" when its env vars are set (RESEND_API_KEY, or
MAILGUN_API_KEY + MAILGUN_DOMAIN) — read at CALL time so a running service
picks up rotated keys and tests can toggle configuration per test.

Failure taxonomy (mirrors content_client's Transient/permanent split — the
consumer maps it onto the shared retry/backoff -> DLQ path):
  - MailerTransientError — network unreachable, timeout, HTTP 429/5xx.
    send() falls through to the next configured provider; if every provider
    failed and ANY failure was transient, send() raises transient so the
    broker redelivers (the provider may recover).
  - MailerError — permanent: HTTP 4xx (bad recipient, bad key), or no
    provider configured at all. The consumer concludes the notification as
    failed-and-logged; retrying would not help.

Base URLs are env-overridable (RESEND_API_BASE_URL / MAILGUN_API_BASE_URL)
so tests point them at a dead address — an unmocked call fails instantly
instead of touching the real providers.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("notification.mailer")

RESEND_API_BASE_URL = os.getenv("RESEND_API_BASE_URL", "https://api.resend.com")
MAILGUN_API_BASE_URL = os.getenv("MAILGUN_API_BASE_URL", "https://api.mailgun.net")
TIMEOUT_SECONDS = float(os.getenv("MAILER_TIMEOUT_SECONDS", "10"))


class MailerError(Exception):
    """Permanent send failure — retrying the same message would not help."""


class MailerTransientError(MailerError):
    """Retryable send failure — network trouble or provider 429/5xx."""


def _resend_key() -> str:
    return os.getenv("RESEND_API_KEY", "")


def _mailgun_key() -> str:
    return os.getenv("MAILGUN_API_KEY", "")


def _mailgun_domain() -> str:
    return os.getenv("MAILGUN_DOMAIN", "")


def from_address() -> str:
    """Sender identity — set NOTIFY_FROM_EMAIL to a domain verified with the
    provider (Resend/Mailgun reject unverified senders with a 4xx)."""
    return os.getenv("NOTIFY_FROM_EMAIL", "CreditFlow <no-reply@creditflow.local>")


def _classify(status_code: int, provider: str, detail: str) -> MailerError:
    if status_code == 429 or status_code >= 500:
        return MailerTransientError(f"{provider} HTTP {status_code}: {detail}")
    return MailerError(f"{provider} HTTP {status_code}: {detail}")


def send_via_resend(to: str, subject: str, text: str, html: str | None = None) -> str:
    """POST /emails on the Resend API. Returns the provider message id."""
    payload: dict = {"from": from_address(), "to": [to], "subject": subject, "text": text}
    if html:
        payload["html"] = html
    try:
        resp = httpx.post(
            f"{RESEND_API_BASE_URL}/emails",
            headers={"Authorization": f"Bearer {_resend_key()}"},
            json=payload,
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise MailerTransientError(f"resend unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise _classify(resp.status_code, "resend", resp.text[:300])
    return str(resp.json().get("id", ""))


def send_via_mailgun(to: str, subject: str, text: str, html: str | None = None) -> str:
    """POST /v3/{domain}/messages on the Mailgun API. Returns the message id."""
    data: dict = {"from": from_address(), "to": to, "subject": subject, "text": text}
    if html:
        data["html"] = html
    try:
        resp = httpx.post(
            f"{MAILGUN_API_BASE_URL}/v3/{_mailgun_domain()}/messages",
            auth=("api", _mailgun_key()),
            data=data,
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise MailerTransientError(f"mailgun unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise _classify(resp.status_code, "mailgun", resp.text[:300])
    return str(resp.json().get("id", ""))


def send(to: str, subject: str, text: str, html: str | None = None) -> tuple[str, str]:
    """Send through the first provider that accepts the message — Resend
    primary, Mailgun fallback. Returns (provider, provider_message_id)."""
    failures: list[tuple[str, MailerError]] = []

    if _resend_key():
        try:
            return "resend", send_via_resend(to, subject, text, html=html)
        except MailerError as exc:
            logger.warning("resend send failed: %s", exc)
            failures.append(("resend", exc))

    if _mailgun_key() and _mailgun_domain():
        try:
            return "mailgun", send_via_mailgun(to, subject, text, html=html)
        except MailerError as exc:
            logger.warning("mailgun send failed: %s", exc)
            failures.append(("mailgun", exc))

    if not failures:
        raise MailerError(
            "no email provider configured — set RESEND_API_KEY and/or "
            "MAILGUN_API_KEY + MAILGUN_DOMAIN"
        )

    summary = "; ".join(f"{provider}: {exc}" for provider, exc in failures)
    if any(isinstance(exc, MailerTransientError) for _, exc in failures):
        raise MailerTransientError(summary)
    raise MailerError(summary)
