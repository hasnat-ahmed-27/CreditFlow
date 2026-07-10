"""
Invite-token helpers — same rule as Auth's one-time tokens: the raw urlsafe
token goes into the invite email, only its SHA-256 hash is stored. These are
high-entropy random values (not passwords), so a fast non-salted hash is safe
and allows exact-match lookup.
"""
from __future__ import annotations

import hashlib
import secrets


def new_invite_token() -> str:
    """256 bits of randomness, urlsafe — goes into the emailed invite link."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """What we store/look up in the DB instead of the raw token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
