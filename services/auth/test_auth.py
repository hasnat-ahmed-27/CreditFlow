"""
Auth service tests: signup, verification, login (success/failure), JWT claims
via jwt_utils, Redis jti sessions, refresh rotation + reuse detection,
logout/revoke, password reset, rate limiting.
"""
from __future__ import annotations

import uuid

from creditflow_common import jwt_utils

import store


def _signup(client, email: str, password: str = "s3cretpass!") -> dict:
    r = client.post("/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def _signup_and_verify(client, email: str, password: str = "s3cretpass!") -> None:
    body = _signup(client, email, password)
    r = client.post("/auth/verify-email", json={"token": body["dev_verification_token"]})
    assert r.status_code == 200, r.text


def _login(client, email: str, password: str = "s3cretpass!") -> dict:
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _unique_email() -> str:
    return f"u{uuid.uuid4().hex[:10]}@test.dev"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "auth"}


def test_signup_publishes_user_registered(client, published_events):
    email = _unique_email()
    body = _signup(client, email)
    assert body["email"] == email
    keys = [rk for rk, _ in published_events]
    assert keys == ["user.registered"]
    payload = published_events[0][1]
    assert payload["user_id"] == body["user_id"]
    assert payload["email"] == email
    assert payload["verification_token"]  # notification service emails this


def test_signup_duplicate_email_conflict(client):
    email = _unique_email()
    _signup(client, email)
    r = client.post("/auth/signup", json={"email": email, "password": "s3cretpass!"})
    assert r.status_code == 409


def test_login_requires_verified_email(client):
    email = _unique_email()
    _signup(client, email)
    r = client.post("/auth/login", json={"email": email, "password": "s3cretpass!"})
    assert r.status_code == 403


def test_login_success_issues_valid_tokens(client, published_events):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)

    # Access token: verifiable with the public key via the shared lib, and
    # carries the payload the spec requires.
    claims = jwt_utils.verify_token(tokens["access_token"])
    assert claims["type"] == "access"
    assert claims["sub"] and claims["account_id"] and claims["jti"]
    assert claims["role"] == "owner"
    # Its jti is live in Redis (this is what admin/gateway will check).
    assert store.session_exists(claims["jti"])

    refresh_claims = jwt_utils.verify_token(tokens["refresh_token"])
    assert refresh_claims["type"] == "refresh"

    assert "user.logged_in" in [rk for rk, _ in published_events]

    # And the token works against a protected route.
    r = client.get("/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 200
    assert r.json()["user_id"] == claims["sub"]


def test_login_wrong_password(client):
    email = _unique_email()
    _signup_and_verify(client, email)
    r = client.post("/auth/login", json={"email": email, "password": "wrong-password"})
    assert r.status_code == 401


def test_login_unknown_email(client):
    r = client.post("/auth/login", json={"email": _unique_email(), "password": "whatever123"})
    assert r.status_code == 401


def test_me_rejects_garbage_and_refresh_tokens(client):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)
    assert client.get("/me", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401
    # A (valid) refresh token must not pass as an access token.
    r = client.get("/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
    assert r.status_code == 401


def test_refresh_rotates_and_detects_reuse(client):
    email = _unique_email()
    _signup_and_verify(client, email)
    first = _login(client, email)
    old_access_jti = jwt_utils.verify_token(first["access_token"])["jti"]

    # Rotate: new pair, old access session revoked in Redis.
    r = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert not store.session_exists(old_access_jti)
    new_access_jti = jwt_utils.verify_token(second["access_token"])["jti"]
    assert store.session_exists(new_access_jti)

    # Replaying the ALREADY-ROTATED refresh token = reuse -> everything revoked.
    r = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401
    assert not store.session_exists(new_access_jti)
    r = client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert r.status_code == 401


def test_refresh_rejects_access_token(client):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)
    r = client.post("/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert r.status_code == 401


def test_logout_revokes_session_immediately(client):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert client.get("/me", headers=headers).status_code == 200
    r = client.post("/auth/logout", headers=headers, json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    # Token is cryptographically still valid but its jti is gone from Redis.
    assert client.get("/me", headers=headers).status_code == 401
    # The revoked refresh token can't be used either.
    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401


def test_password_reset_flow(client, published_events):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)

    r = client.post("/auth/password-reset/request", json={"email": email})
    assert r.status_code == 202
    reset_token = r.json()["dev_reset_token"]
    assert "user.password_reset_requested" in [rk for rk, _ in published_events]

    r = client.post("/auth/password-reset/confirm",
                    json={"token": reset_token, "new_password": "brand-new-pass1"})
    assert r.status_code == 200

    # Old password dead, new one works, existing sessions were revoked.
    assert client.post("/auth/login", json={"email": email, "password": "s3cretpass!"}).status_code == 401
    old_jti = jwt_utils.verify_token(tokens["access_token"])["jti"]
    assert not store.session_exists(old_jti)
    _login(client, email, "brand-new-pass1")

    # Reset token is single-use.
    r = client.post("/auth/password-reset/confirm",
                    json={"token": reset_token, "new_password": "another-pass123"})
    assert r.status_code == 400


def test_password_reset_request_does_not_leak_existence(client):
    r = client.post("/auth/password-reset/request", json={"email": _unique_email()})
    assert r.status_code == 202
    assert "dev_reset_token" not in r.json()


def test_login_rate_limited_per_email(client):
    email = _unique_email()
    _signup_and_verify(client, email)
    for _ in range(5):  # AUTH_LOGIN_MAX_ATTEMPTS=5 in conftest
        r = client.post("/auth/login", json={"email": email, "password": "wrong-password"})
        assert r.status_code == 401
    r = client.post("/auth/login", json={"email": email, "password": "wrong-password"})
    assert r.status_code == 429
    # Even the CORRECT password is blocked while the window is hot.
    r = client.post("/auth/login", json={"email": email, "password": "s3cretpass!"})
    assert r.status_code == 429
