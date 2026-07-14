"""
Synchronous quota gate — the spec's "accept a generation request only after a
synchronous quota check against Usage Service".

We call Usage's POST /usage/check with the CALLER'S bearer token (Usage scopes
the check to the token's account_id claim, so no account id travels in the
body) and branch on the `allowed` flag; the check itself always answers 200.

Failure posture: fail CLOSED. If Usage is unreachable we cannot know whether
the account has headroom, and an OpenRouter call spends real money — so the
request is refused (503) rather than waved through.
"""
from __future__ import annotations

import httpx

from creditflow_common.config import env

USAGE_URL = env("USAGE_URL", "http://usage:8000")


class QuotaCheckError(Exception):
    """Usage service unreachable or answered abnormally — caller returns 503."""


def estimate_tokens(text: str) -> int:
    """Cheap pre-flight estimate (~4 chars/token) — the real counts come from
    OpenRouter's usage accounting after the stream ends."""
    return max(1, len(text) // 4)


async def check_quota(bearer_token: str, estimated_tokens: int) -> dict:
    """Returns Usage's verdict dict ({"allowed": bool, ...}); raises
    QuotaCheckError if the check could not be performed at all."""
    try:
        async with httpx.AsyncClient(base_url=USAGE_URL, timeout=5.0) as client:
            resp = await client.post(
                "/usage/check",
                json={"estimated_tokens": estimated_tokens},
                headers={"Authorization": f"Bearer {bearer_token}"},
            )
    except httpx.HTTPError as exc:
        raise QuotaCheckError(f"usage service unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise QuotaCheckError(f"usage service answered {resp.status_code}: {resp.text[:200]}")
    return resp.json()
