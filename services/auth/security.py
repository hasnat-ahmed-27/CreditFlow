"""
Password hashing + one-time-token helpers.

- Passwords: argon2id via passlib (spec: bcrypt or argon2). Argon2 is the
  current OWASP first choice and has no 72-byte input limit like bcrypt.
- One-time tokens (email verification / password reset): a random urlsafe
  string is sent to the user, but only its SHA-256 hash is stored. Unlike
  passwords these tokens are high-entropy random values, so a fast
  non-salted hash is safe and lets us look them up by exact match.
"""
from __future__ import annotations

import hashlib
import secrets

from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(password, password_hash)
    except ValueError:
        return False


def new_one_time_token() -> str:
    """256 bits of randomness, urlsafe — goes into the emailed link."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """What we store/look up in the DB instead of the raw token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
