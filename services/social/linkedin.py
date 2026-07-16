"""
LinkedIn API client — the ONE module that talks to LinkedIn, so tests mock
exactly this seam (conftest monkeypatches these functions AND points both
base URLs at a dead address as a belt-and-braces guard, the same pattern the
AI service uses for OpenRouter).

Endpoints used (spec: OAuth 2.0 / OIDC + UGC Posts + image upload):
  - {OAUTH}/authorization           built, not called — the user's browser goes there
  - {OAUTH}/accessToken             authorization-code exchange (form-encoded)
  - {API}/userinfo                  OIDC userinfo -> member id (`sub`) for the author URN
  - {API}/assets?action=registerUpload   register the image upload -> uploadUrl + asset URN
  - PUT {uploadUrl}                 raw binary upload (URL comes from the register call)
  - {API}/ugcPosts                  create the post (text-only, or text+image via asset URN)

Every developer registers their own LinkedIn app (spec) — client id/secret/
redirect URI arrive via env and are read at CALL time so the build-time
import smoke test needs no secrets. Scopes per spec: openid profile email
w_member_social.

Error taxonomy for callers: LinkedInTransientError (network trouble, 429,
5xx — safe to retry; the consumer re-raises so the broker's bounded
retry/backoff and DLQ apply) vs LinkedInError (everything else — permanent,
recorded on the publish job and emitted as post.failed). Access tokens are
passed straight through to headers and never logged; error details keep only
a truncated response BODY.
"""
from __future__ import annotations

import os
from urllib.parse import urlencode

import httpx

from creditflow_common.config import env

OAUTH_BASE_URL = env("LINKEDIN_OAUTH_BASE_URL", "https://www.linkedin.com/oauth/v2")
API_BASE_URL = env("LINKEDIN_API_BASE_URL", "https://api.linkedin.com/v2")

SCOPES = "openid profile email w_member_social"

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class LinkedInError(Exception):
    """Permanent upstream failure — bad request, revoked token, 4xx."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LinkedInTransientError(LinkedInError):
    """Retryable upstream failure — network error, 429, 5xx."""


def _client_id() -> str:
    # Read at call time (not import) so tests and the build-time import smoke
    # test never require the app registration.
    val = os.getenv("LINKEDIN_CLIENT_ID", "")
    if not val:
        raise LinkedInError("LINKEDIN_CLIENT_ID is not configured")
    return val


def _client_secret() -> str:
    val = os.getenv("LINKEDIN_CLIENT_SECRET", "")
    if not val:
        raise LinkedInError("LINKEDIN_CLIENT_SECRET is not configured")
    return val


def _redirect_uri() -> str:
    val = os.getenv("LINKEDIN_REDIRECT_URI", "")
    if not val:
        raise LinkedInError("LINKEDIN_REDIRECT_URI is not configured")
    return val


def _checked(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code == 429 or resp.status_code >= 500:
        raise LinkedInTransientError(
            f"{what}: LinkedIn HTTP {resp.status_code}: {resp.text[:300]}",
            status_code=resp.status_code,
        )
    if resp.status_code >= 400:
        raise LinkedInError(
            f"{what}: LinkedIn HTTP {resp.status_code}: {resp.text[:300]}",
            status_code=resp.status_code,
        )
    return resp


def authorization_url(state: str) -> str:
    """The LinkedIn consent URL the frontend redirects the user to. No HTTP."""
    query = urlencode({
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "state": state,
        "scope": SCOPES,
    })
    return f"{OAUTH_BASE_URL}/authorization?{query}"


def exchange_code(code: str) -> dict:
    """Authorization code -> tokens. Returns LinkedIn's token payload:
    access_token, expires_in, scope, and (when the app is provisioned for
    them) refresh_token / refresh_token_expires_in."""
    try:
        resp = httpx.post(
            f"{OAUTH_BASE_URL}/accessToken",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
                "client_id": _client_id(),
                "client_secret": _client_secret(),
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise LinkedInTransientError(f"token exchange failed: {exc}") from exc
    return _checked(resp, "token exchange").json()


def get_userinfo(access_token: str) -> dict:
    """OIDC userinfo — `sub` is the member id behind urn:li:person:{sub}."""
    try:
        resp = httpx.get(
            f"{API_BASE_URL}/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise LinkedInTransientError(f"userinfo failed: {exc}") from exc
    return _checked(resp, "userinfo").json()


def register_image_upload(access_token: str, owner_urn: str) -> dict:
    """Step 1 of the image flow: returns {"upload_url", "asset_urn"}."""
    body = {
        "registerUploadRequest": {
            "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
            "owner": owner_urn,
            "serviceRelationships": [
                {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
            ],
        }
    }
    try:
        resp = httpx.post(
            f"{API_BASE_URL}/assets?action=registerUpload",
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise LinkedInTransientError(f"register upload failed: {exc}") from exc
    value = _checked(resp, "register upload").json().get("value") or {}
    mechanism = (value.get("uploadMechanism") or {}).get(
        "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest") or {}
    upload_url = mechanism.get("uploadUrl")
    asset_urn = value.get("asset")
    if not upload_url or not asset_urn:
        raise LinkedInError("register upload: response missing uploadUrl/asset")
    return {"upload_url": upload_url, "asset_urn": asset_urn}


def upload_image_binary(access_token: str, upload_url: str, data: bytes,
                        content_type: str | None = None) -> None:
    """Step 2: PUT the raw bytes to the URL the register call handed back."""
    headers = {"Authorization": f"Bearer {access_token}"}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        resp = httpx.put(upload_url, content=data, headers=headers, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise LinkedInTransientError(f"image upload failed: {exc}") from exc
    _checked(resp, "image upload")


def create_post(access_token: str, author_urn: str, text: str,
                asset_urn: str | None = None) -> dict:
    """Step 3 (or the whole flow for text-only): create the UGC post.
    Returns {"post_id", "post_url"} — post_id is the URN LinkedIn assigns."""
    share_content: dict = {
        "shareCommentary": {"text": text},
        "shareMediaCategory": "IMAGE" if asset_urn else "NONE",
    }
    if asset_urn:
        share_content["media"] = [{"status": "READY", "media": asset_urn}]
    body = {
        "author": author_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {"com.linkedin.ugc.ShareContent": share_content},
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    try:
        resp = httpx.post(
            f"{API_BASE_URL}/ugcPosts",
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise LinkedInTransientError(f"create post failed: {exc}") from exc
    _checked(resp, "create post")
    post_id = resp.headers.get("X-RestLi-Id") or (resp.json() or {}).get("id")
    if not post_id:
        raise LinkedInError("create post: response missing the post id")
    return {
        "post_id": post_id,
        "post_url": f"https://www.linkedin.com/feed/update/{post_id}/",
    }
