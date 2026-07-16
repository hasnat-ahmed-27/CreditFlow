"""
Read-only REST clients for the aggregate per-account view — the spec's
"plan tier, credit balance, usage this period, active members — pulled from
Credits, Usage, and User services (read-only calls, no writes)".

All cross-service HTTP for this service lives in this ONE module (the
mockable seam, same rule as social/content_client.py) — tests replace these
four functions and ALSO point the base URLs at a dead address so an unmocked
call fails instantly instead of touching the network.

Auth story: every downstream read endpoint is scoped by the CALLER's token
(Credits/Usage read the account_id claim; User checks membership), so we
FORWARD the admin console caller's bearer instead of minting anything — this
service holds only the public key. KNOWN GAP (same family as Scheduler and
Social recorded): there is no service-auth token yet, so a SuperAdmin
inspecting a FOREIGN account gets that section from the token's own scope or
a 4xx — the overview route degrades per-section (see routes.py) rather than
failing, and the directory/audit/session data (which we own or read from
Redis) is always complete. When the service-auth story lands, forward that
token here instead — no route changes needed.
"""
from __future__ import annotations

import os

import httpx

USER_URL = os.getenv("USER_URL", "http://user:8000")
CREDITS_URL = os.getenv("CREDITS_URL", "http://credits:8000")
USAGE_URL = os.getenv("USAGE_URL", "http://usage:8000")

TIMEOUT_SECONDS = 10.0


class ClientError(Exception):
    """Any downstream failure — network, timeout, or non-2xx response."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _get(base_url: str, path: str, bearer_token: str) -> dict:
    try:
        resp = httpx.get(
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ClientError(f"GET {path}: {exc}") from exc
    if resp.status_code >= 400:
        raise ClientError(f"GET {path}: HTTP {resp.status_code}", status_code=resp.status_code)
    return resp.json()


def get_account(account_id: str, bearer_token: str) -> dict:
    """Account profile (name, plan tier, seat count) from the User service."""
    return _get(USER_URL, f"/accounts/{account_id}", bearer_token)


def get_members(account_id: str, bearer_token: str) -> dict:
    """Active members of the account from the User service."""
    return _get(USER_URL, f"/accounts/{account_id}/members", bearer_token)


def get_credit_balance(bearer_token: str) -> dict:
    """Credit balance from the Credits service (scoped by the token's account)."""
    return _get(CREDITS_URL, "/credits/balance", bearer_token)


def get_usage_summary(bearer_token: str) -> dict:
    """Usage this period from the Usage service (scoped by the token's account)."""
    return _get(USAGE_URL, "/usage/summary", bearer_token)
