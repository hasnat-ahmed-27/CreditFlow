"""
Inbound webhook signature verification (spec §8 Service 1: "Expose webhook
endpoints for Stripe, LinkedIn, and OpenRouter; verify signatures before
acting").

Every verifier here works on the RAW REQUEST BYTES. Re-serialising parsed
JSON changes key order and whitespace and would break any HMAC — webhooks.py
therefore reads `await request.body()` once and passes those exact bytes both
to the verifier and downstream.

All comparisons use `hmac.compare_digest`. A missing secret is a hard
failure, never a pass: an unconfigured endpoint must not become an
unauthenticated one.

Implemented WITHOUT the Stripe SDK on purpose. The scheme is a documented
HMAC construction, the gateway would otherwise pull the whole SDK in for one
function, and — the deciding reason — a hand-written verifier is testable
with a locally computed signature, so the CI suite proves both the valid and
the tampered case with no secrets and no network.

PER-PROVIDER APPROACH
---------------------
Stripe (fully specified, implemented exactly):
    Stripe-Signature: t=<unix-ts>,v1=<hex>[,v1=<hex>...]
    signed payload   = "<t>.<raw body>"
    expected         = HMAC-SHA256(secret, signed payload), hex
  The timestamp is inside the MAC, so a replayed body cannot be re-dated; we
  additionally reject anything older than `tolerance` seconds, which is what
  bounds replay. Multiple v1 values appear during a secret rotation — any
  match is accepted.

LinkedIn (Event Notifications):
    LinkedIn signs the raw body with the APP'S CLIENT SECRET using
    HMAC-SHA256 and sends it base64-encoded in `X-LI-Signature`. That is what
    `verify_linkedin` checks. LinkedIn also validates a new endpoint with a
    one-off GET carrying `challengeCode`, answered with the HMAC of that code
    — not implemented here because it is a one-time registration handshake
    performed against a registered LinkedIn app, which this project does not
    have credentials for; the same `_verify_hmac` primitive answers it in one
    line when it does.

OpenRouter:
    OpenRouter does not publish a signature scheme for inbound callbacks. We
    apply the conventional one — HMAC-SHA256 over the raw body with a shared
    secret, hex, optionally prefixed `sha256=` (the GitHub-style spelling
    most providers converged on) in `X-OpenRouter-Signature`. If the provider
    later documents something else, only `verify_openrouter` changes; the
    endpoint, dedup, and publish path do not.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time


class SignatureError(Exception):
    """Signature absent, malformed, stale, or wrong."""


DEFAULT_TOLERANCE_SECONDS = 300


def _require_secret(secret: str, provider: str) -> None:
    if not secret:
        raise SignatureError(f"{provider} webhook secret is not configured")


def _expected_hex(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_stripe(
    payload: bytes,
    header: str,
    secret: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> None:
    """Raise SignatureError unless `header` is a valid Stripe-Signature for
    these exact bytes, signed with `secret`, and recent enough."""
    _require_secret(secret, "Stripe")
    if not header:
        raise SignatureError("missing Stripe-Signature header")

    timestamp: str | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)
    if timestamp is None or not candidates:
        raise SignatureError("malformed Stripe-Signature header")

    try:
        signed_at = int(timestamp)
    except ValueError as exc:
        raise SignatureError("malformed Stripe-Signature timestamp") from exc
    if tolerance_seconds > 0 and abs(time.time() - signed_at) > tolerance_seconds:
        raise SignatureError("Stripe-Signature timestamp outside tolerance")

    expected = _expected_hex(secret, f"{signed_at}.".encode("utf-8") + payload)
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise SignatureError("Stripe signature mismatch")


def stripe_signature_header(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a valid Stripe-Signature for `payload`. Used by the test suite to
    sign its own fixtures (no Stripe account, no secrets in CI) and available
    for local end-to-end pokes at the endpoint."""
    signed_at = int(time.time()) if timestamp is None else timestamp
    signature = _expected_hex(secret, f"{signed_at}.".encode("utf-8") + payload)
    return f"t={signed_at},v1={signature}"


def _verify_hmac(payload: bytes, header: str, secret: str, provider: str, encoding: str) -> None:
    """Shared HMAC-SHA256-over-raw-body check. `encoding` is how the provider
    spells the digest on the wire: 'base64' (LinkedIn) or 'hex' (OpenRouter,
    with an optional `sha256=` prefix)."""
    _require_secret(secret, provider)
    if not header:
        raise SignatureError(f"missing {provider} signature header")

    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    presented = header.split("=", 1)[1] if header.startswith("sha256=") else header
    if encoding == "base64":
        expected = base64.b64encode(digest).decode("ascii")
    else:
        expected = digest.hex()

    try:
        if not hmac.compare_digest(expected, presented.strip()):
            raise SignatureError(f"{provider} signature mismatch")
    except (binascii.Error, ValueError) as exc:  # non-ASCII / undecodable header
        raise SignatureError(f"malformed {provider} signature header") from exc


def verify_linkedin(payload: bytes, header: str, secret: str) -> None:
    _verify_hmac(payload, header, secret, "LinkedIn", encoding="base64")


def linkedin_signature_header(payload: bytes, secret: str) -> str:
    """Test/dev counterpart of verify_linkedin."""
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    ).decode("ascii")


def verify_openrouter(payload: bytes, header: str, secret: str) -> None:
    _verify_hmac(payload, header, secret, "OpenRouter", encoding="hex")


def openrouter_signature_header(payload: bytes, secret: str) -> str:
    """Test/dev counterpart of verify_openrouter."""
    return "sha256=" + _expected_hex(secret, payload)
