"""
jwt_utils.py — RS256 JWT signing + verification.

Design (multi-service):
  - ONLY the Auth service loads the PRIVATE key and calls `sign_access_token` /
    `sign_refresh_token`.
  - EVERY service loads the PUBLIC key and calls `verify_token` to validate
    tokens without any shared secret.

Every access token carries: sub (user_id), account_id, role, jti, iss, iat, exp.
The `jti` is what the Auth/Admin services track in Redis for active-session
listing and revocation.
"""
from __future__ import annotations

import time
import uuid
from functools import lru_cache
from typing import Any

import jwt  # PyJWT

from . import config


@lru_cache(maxsize=1)
def _private_key() -> str:
    with open(config.JWT_PRIVATE_KEY_PATH, "r", encoding="utf-8") as f:
        return f.read()


@lru_cache(maxsize=1)
def _public_key() -> str:
    with open(config.JWT_PUBLIC_KEY_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _sign(payload: dict[str, Any], ttl: int) -> str:
    now = int(time.time())
    body = {
        **payload,
        "iss": config.JWT_ISSUER,
        "iat": now,
        "exp": now + ttl,
        "jti": payload.get("jti") or str(uuid.uuid4()),
    }
    return jwt.encode(body, _private_key(), algorithm=config.JWT_ALGORITHM)


def sign_access_token(user_id: str, account_id: str, role: str, jti: str | None = None) -> tuple[str, str]:
    """Return (token, jti). jti lets Auth store the session in Redis for revocation."""
    jti = jti or str(uuid.uuid4())
    token = _sign(
        {"sub": user_id, "account_id": account_id, "role": role, "type": "access", "jti": jti},
        config.JWT_ACCESS_TTL_SECONDS,
    )
    return token, jti


def sign_refresh_token(user_id: str, account_id: str, jti: str | None = None) -> tuple[str, str]:
    jti = jti or str(uuid.uuid4())
    token = _sign(
        {"sub": user_id, "account_id": account_id, "type": "refresh", "jti": jti},
        config.JWT_REFRESH_TTL_SECONDS,
    )
    return token, jti


class TokenError(Exception):
    """Raised when a token is missing, expired, or invalid."""


def verify_token(token: str) -> dict[str, Any]:
    """Verify signature + expiry with the PUBLIC key. Returns the claims dict."""
    try:
        return jwt.decode(
            token,
            _public_key(),
            algorithms=[config.JWT_ALGORITHM],
            issuer=config.JWT_ISSUER,
            options={"require": ["exp", "iat", "iss", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"invalid token: {exc}") from exc
