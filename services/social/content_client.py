"""
Content-service REST client — how this service turns a content_id into the
authoritative post body and image bytes. Isolated like linkedin.py so tests
mock exactly this seam (CONTENT_URL also points at a dead address in tests).

Auth: the Content service tenant-scopes everything by the caller's JWT, so
every function takes a bearer token. The manual POST /publish route forwards
the CALLER's own token (same pattern as the AI service calling Usage) — full
fidelity, works today. The consumer has no user request behind it; see
consumer.py for how it copes until the service-auth story exists.

get_image accepts whatever content.image_url holds: an ABSOLUTE URL
(AI-generated images live on external hosts) is fetched directly with no
Authorization header — never leak our JWT to third parties; a RELATIVE path
(`/content/{id}/image`, the Content service's uploaded-media route) is
resolved against CONTENT_URL and fetched with the bearer.

Error taxonomy mirrors linkedin.py: ContentTransientError (network, 429,
5xx) is retryable; ContentClientError (with .status_code) is permanent.
"""
from __future__ import annotations

import httpx

from creditflow_common.config import env

CONTENT_URL = env("CONTENT_URL", "http://content:8000")

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ContentClientError(Exception):
    """Permanent failure — 4xx from the Content service (or a bad URL)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ContentTransientError(ContentClientError):
    """Retryable failure — network error, 429, 5xx."""


def _checked(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code == 429 or resp.status_code >= 500:
        raise ContentTransientError(f"{what}: HTTP {resp.status_code}", status_code=resp.status_code)
    if resp.status_code >= 400:
        raise ContentClientError(f"{what}: HTTP {resp.status_code}", status_code=resp.status_code)
    return resp


def get_content(content_id: str, bearer_token: str) -> dict:
    """GET /content/{id} — the authoritative record (title, body, status,
    image_url, ...), tenant-scoped by the token."""
    try:
        resp = httpx.get(
            f"{CONTENT_URL}/content/{content_id}",
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ContentTransientError(f"content fetch failed: {exc}") from exc
    return _checked(resp, f"content {content_id}").json()


def get_image(image_url: str, bearer_token: str | None = None) -> tuple[bytes, str]:
    """Fetch image bytes; returns (data, content_type). See module docstring
    for the absolute-vs-relative URL rules."""
    if image_url.startswith(("http://", "https://")):
        url, headers = image_url, {}
    else:
        url = f"{CONTENT_URL}{image_url if image_url.startswith('/') else '/' + image_url}"
        headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    try:
        resp = httpx.get(url, headers=headers, timeout=_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise ContentTransientError(f"image fetch failed: {exc}") from exc
    _checked(resp, f"image {image_url}")
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


def set_status(content_id: str, status: str, bearer_token: str) -> dict:
    """POST /content/{id}/status — advance the Content-side lifecycle (the
    Content service's docs say `published` is normally set by this service).
    Callers treat this as best-effort."""
    try:
        resp = httpx.post(
            f"{CONTENT_URL}/content/{content_id}/status",
            json={"status": status},
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ContentTransientError(f"status change failed: {exc}") from exc
    return _checked(resp, f"content {content_id} status -> {status}").json()
