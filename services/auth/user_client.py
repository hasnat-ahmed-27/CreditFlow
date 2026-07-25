"""
The Auth -> User service seam: where the `account_id` and `role` claims come
from (spec §6 — "JWT must carry user_id, account_id, role, and jti").

All cross-service HTTP for this service lives in this ONE module — the
mockable seam, same rule as ai/usage_client.py and admin/clients.py. Tests
replace these functions AND point USER_URL at a dead address, so an unmocked
call fails instantly instead of touching the network.

The decision (also documented in services/user/internal.py): Auth asks the
User service SYNCHRONOUSLY rather than maintaining an event-fed read model,
because account_members is the User service's table and a projection would be
stale at exactly the wrong moment — the instant a token is minted. This
mirrors the AI service's synchronous quota gate against Usage: decisions that
must be current are asked for, not remembered.

Failure posture per call site (see routes.py):
  - signup      -> best effort; the `user.registered` consumer provisions the
                   account asynchronously anyway, so a blip costs nothing.
  - login       -> fail CLOSED (503). We must never fall back to minting the
                   old account_id == user_id placeholder: every other service
                   scopes real data by that claim, so a wrong account_id is a
                   cross-tenant data leak, not a degraded experience.
  - switch      -> fail closed (503) — the same reasoning.
  - refresh     -> fall back to the role stored on the refresh row. Refresh is
                   the frontend's silent-renewal path; a User outage must not
                   log the whole platform out. A DEFINITE "not a member"
                   (404) still ends the session.
"""
from __future__ import annotations

import httpx

from creditflow_common.config import env

USER_URL = env("USER_URL", "http://user:8000").rstrip("/")
TIMEOUT_SECONDS = 5.0


class UserServiceError(Exception):
    """User service unreachable or answered abnormally — the caller decides
    whether that is fatal (login/switch) or survivable (signup/refresh)."""


def _raise_for(path: str, resp: httpx.Response) -> None:
    raise UserServiceError(f"{path}: HTTP {resp.status_code}: {resp.text[:200]}")


def ensure_individual_account(user_id: str, email: str) -> dict:
    """Idempotently provision (or look up) the user's individual account.
    Returns {"account_id", "role", ...}. Raises UserServiceError on failure."""
    try:
        resp = httpx.post(
            f"{USER_URL}/internal/accounts/individual",
            json={"user_id": user_id, "email": email},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise UserServiceError(f"user service unreachable: {exc}") from exc
    if resp.status_code not in (200, 201):
        _raise_for("POST /internal/accounts/individual", resp)
    return resp.json()


def get_membership(user_id: str, account_id: str) -> dict | None:
    """The user's role in `account_id`, or None if they are not a member
    (the User service answers 404 for both "no such account" and "not a
    member" — Auth needs no more than that). Raises UserServiceError if the
    question could not be asked at all."""
    try:
        resp = httpx.get(
            f"{USER_URL}/internal/users/{user_id}/accounts/{account_id}",
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise UserServiceError(f"user service unreachable: {exc}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        _raise_for("GET /internal/users/{user_id}/accounts/{account_id}", resp)
    return resp.json()
