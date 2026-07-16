"""
Token-at-rest encryption — Fernet (the spec's named choice), symmetric key
from the SOCIAL_TOKEN_ENCRYPTION_KEY environment variable (a standard Fernet
key: 32 url-safe base64 bytes, e.g. `python -c "from cryptography.fernet
import Fernet; print(Fernet.generate_key().decode())"`).

The key is read at CALL time, not import time, so the Docker build-time
import smoke test and route modules never require the secret. LinkedIn
access/refresh tokens exist in plaintext only in memory between the OAuth
exchange and encryption (and between decryption and the outbound LinkedIn
call) — they are never logged and never returned by any endpoint.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(Exception):
    """Missing/invalid encryption key or undecryptable ciphertext."""


def _fernet() -> Fernet:
    key = os.getenv("SOCIAL_TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise CryptoError("SOCIAL_TOKEN_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.encode("utf-8"))
    except ValueError as exc:
        raise CryptoError("SOCIAL_TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise CryptoError("stored token cannot be decrypted with the configured key") from exc
