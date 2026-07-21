"""
Auth service tests: signup, verification, login (success/failure), REAL
account-scoped JWT claims resolved from the User service, the account
switcher, the platform SuperAdmin role, Redis jti sessions, refresh rotation
+ role re-resolution + reuse detection, logout/revoke, password reset, rate
limiting.

No infra: SQLite + fakeredis via conftest, publisher stubbed, and the User
service replaced by the in-memory `accounts` fixture while USER_URL points at
a dead address.
"""
from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select

from creditflow_common import jwt_utils

import conftest
import store
import superadmin
import user_client
from models import RefreshToken, User


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
    # account_id is the REAL account from the User service, not the user_id.
    assert claims["account_id"] != claims["sub"]
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


# --------------------------------------------------------------------------
# Real account-scoped claims (spec §6) — resolved from the User service
# --------------------------------------------------------------------------

def test_signup_creates_the_individual_account(client, accounts):
    """Spec §8 Service 3: an Account of type 'individual' is created on
    signup, with the user as its Owner."""
    email = _unique_email()
    body = _signup(client, email)

    assert ("ensure_individual_account", body["user_id"], email) in accounts["calls"]
    account_id = accounts["individual"][body["user_id"]]
    assert body["account_id"] == account_id
    assert accounts["memberships"][(body["user_id"], account_id)] == "owner"


def test_login_mints_that_real_account_id_and_role(client, accounts):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)

    claims = jwt_utils.verify_token(tokens["access_token"])
    assert claims["account_id"] == accounts["individual"][claims["sub"]]
    assert claims["role"] == "owner"
    # The response echoes the scope so the frontend needn't decode the token.
    assert tokens["account_id"] == claims["account_id"]
    assert tokens["role"] == "owner"
    # Redis carries the same scope for the Admin console's session viewer.
    assert store.get_redis().get(store.SESSION_PREFIX + claims["jti"])


def test_signup_survives_the_user_service_being_down(client, accounts):
    """Provisioning is best effort at signup — the `user.registered` consumer
    creates the same account asynchronously, so the user is still registered."""
    accounts["errors"]["ensure_individual_account"] = user_client.UserServiceError("boom")
    email = _unique_email()
    body = _signup(client, email)
    assert body["account_id"] is None
    assert body["dev_verification_token"]


def test_login_fails_closed_when_the_account_cannot_be_resolved(client, accounts):
    """A guessed account_id would scope every other service's data to the
    wrong tenant, so login refuses rather than minting a placeholder."""
    email = _unique_email()
    _signup_and_verify(client, email)
    accounts["errors"]["ensure_individual_account"] = user_client.UserServiceError("down")

    r = client.post("/auth/login", json={"email": email, "password": "s3cretpass!"})
    assert r.status_code == 503
    assert "Account service unavailable" in r.json()["detail"]


def test_login_heals_a_missing_account(client, accounts):
    """If the signup-time call AND the consumer both failed, login provisions
    the account before minting rather than issuing a scopeless token."""
    accounts["errors"]["ensure_individual_account"] = user_client.UserServiceError("down")
    email = _unique_email()
    body = _signup(client, email)
    assert body["user_id"] not in accounts["individual"]

    accounts["errors"].clear()
    client.post("/auth/verify-email", json={"token": body["dev_verification_token"]})
    claims = jwt_utils.verify_token(_login(client, email)["access_token"])
    assert claims["account_id"] == accounts["individual"][body["user_id"]]


def test_unmocked_user_service_call_fails_instantly(client):
    """Guard proof: USER_URL points at a dead address, so a test that forgets
    to stub the seam errors out instead of reaching the network."""
    with pytest.raises((user_client.UserServiceError, httpx.HTTPError)):
        conftest.REAL_ENSURE_INDIVIDUAL_ACCOUNT("u1", "u1@test.dev")


# --------------------------------------------------------------------------
# Account switcher (spec §4) — a new account-scoped JWT per account
# --------------------------------------------------------------------------

def _switch(client, access_token: str, account_id: str):
    return client.post("/auth/switch-account", json={"account_id": account_id},
                       headers={"Authorization": f"Bearer {access_token}"})


def test_switch_account_issues_a_token_scoped_to_the_new_account(client, accounts):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)
    first = jwt_utils.verify_token(tokens["access_token"])

    # The user also belongs to a team, as a plain member.
    team_id = accounts["add_membership"](first["sub"], "member")

    r = _switch(client, tokens["access_token"], team_id)
    assert r.status_code == 200, r.text
    switched = jwt_utils.verify_token(r.json()["access_token"])
    assert switched["sub"] == first["sub"]
    assert switched["account_id"] == team_id
    assert switched["role"] == "member"          # the role in THAT account
    assert switched["jti"] != first["jti"]
    assert store.session_exists(switched["jti"])

    # The previous scope stops working the moment the switch lands — one
    # login must not hold two live account scopes at once.
    assert not store.session_exists(first["jti"])
    assert client.get("/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
                      ).status_code == 401
    r = client.get("/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert r.json()["account_id"] == team_id


def test_switch_account_rejects_a_non_member(client, accounts):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)

    # An account that exists but belongs to somebody else, and one that does
    # not exist at all — indistinguishable to the caller, by design.
    stranger = accounts["add_membership"]("someone-else", "owner")
    for account_id in (stranger, str(uuid.uuid4())):
        r = _switch(client, tokens["access_token"], account_id)
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == "Not a member of this account"

    # The caller keeps the session they had — a rejected switch changes nothing.
    claims = jwt_utils.verify_token(tokens["access_token"])
    assert store.session_exists(claims["jti"])


def test_switch_account_requires_a_token_and_survives_user_service_outage(client, accounts):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)
    team_id = accounts["add_membership"](jwt_utils.verify_token(tokens["access_token"])["sub"], "admin")

    assert client.post("/auth/switch-account", json={"account_id": team_id}).status_code == 401

    accounts["errors"]["get_membership"] = user_client.UserServiceError("down")
    r = _switch(client, tokens["access_token"], team_id)
    assert r.status_code == 503  # fail closed — never guess a membership


# --------------------------------------------------------------------------
# Platform SuperAdmin (spec §4, §8 Service 13)
# --------------------------------------------------------------------------

def test_superadmin_email_mints_the_superadmin_role(client, monkeypatch):
    """The designated operator's token carries `role: superadmin` — the exact
    string the Admin service's role gate checks — while still carrying a real
    account_id, so account-scoped services keep working for them."""
    email = _unique_email()
    monkeypatch.setenv("SUPERADMIN_EMAILS", f"other@x.dev, {email.upper()} ")

    _signup_and_verify(client, email)
    claims = jwt_utils.verify_token(_login(client, email)["access_token"])
    assert claims["role"] == superadmin.SUPERADMIN_ROLE == "superadmin"
    assert claims["account_id"]


def test_ordinary_users_are_tenant_scoped_not_superadmin(client, accounts, monkeypatch):
    """TenantAdmin/Member roles come from account_members and never escalate:
    the same user is `owner` of their individual account and `member` of a
    team, and neither is `superadmin`."""
    email = _unique_email()
    monkeypatch.setenv("SUPERADMIN_EMAILS", "someone.else@x.dev")
    _signup_and_verify(client, email)
    tokens = _login(client, email)

    owner_claims = jwt_utils.verify_token(tokens["access_token"])
    assert owner_claims["role"] == "owner"

    team_id = accounts["add_membership"](owner_claims["sub"], "member")
    member_claims = jwt_utils.verify_token(
        _switch(client, tokens["access_token"], team_id).json()["access_token"])
    assert member_claims["role"] == "member"


def test_superadmin_still_cannot_switch_into_a_foreign_account(client, accounts, monkeypatch):
    """The platform role grants cross-account VISIBILITY via the Admin
    service — not membership. Switching still requires being a member."""
    email = _unique_email()
    monkeypatch.setenv("SUPERADMIN_EMAILS", email)
    _signup_and_verify(client, email)
    tokens = _login(client, email)

    foreign = accounts["add_membership"]("another-user", "owner")
    assert _switch(client, tokens["access_token"], foreign).status_code == 403


def test_superadmin_role_survives_an_account_switch(client, accounts, monkeypatch):
    email = _unique_email()
    monkeypatch.setenv("SUPERADMIN_EMAILS", email)
    _signup_and_verify(client, email)
    tokens = _login(client, email)
    user_id = jwt_utils.verify_token(tokens["access_token"])["sub"]

    team_id = accounts["add_membership"](user_id, "member")
    claims = jwt_utils.verify_token(_switch(client, tokens["access_token"], team_id).json()["access_token"])
    assert claims["account_id"] == team_id
    assert claims["role"] == "superadmin"  # platform-level, not account-scoped


def test_superadmin_sync_grants_and_revokes_on_startup(client, db_session, monkeypatch):
    """SUPERADMIN_EMAILS is the source of truth in BOTH directions: an
    existing user gets promoted on the next startup, and one dropped from the
    list is demoted AND has their live sessions revoked immediately."""
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)
    jti = jwt_utils.verify_token(tokens["access_token"])["jti"]
    assert jwt_utils.verify_token(tokens["access_token"])["role"] == "owner"

    # Promote an already-registered user.
    monkeypatch.setenv("SUPERADMIN_EMAILS", email)
    assert superadmin.sync(db_session) == {"granted": 1, "revoked": 0}
    db_session.expire_all()
    assert db_session.scalar(select(User).where(User.email == email)).is_superadmin is True
    assert jwt_utils.verify_token(_login(client, email)["access_token"])["role"] == "superadmin"

    # Demote by removing them from the list — the old session dies with it.
    monkeypatch.setenv("SUPERADMIN_EMAILS", "")
    assert superadmin.sync(db_session) == {"granted": 0, "revoked": 1}
    db_session.expire_all()
    assert db_session.scalar(select(User).where(User.email == email)).is_superadmin is False
    assert not store.session_exists(jti)


# --------------------------------------------------------------------------
# Refresh re-resolves the role for the scope it carries
# --------------------------------------------------------------------------

def test_refresh_picks_up_a_role_change(client, accounts):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)
    claims = jwt_utils.verify_token(tokens["access_token"])

    # The user is promoted in the User service after the token was minted.
    accounts["memberships"][(claims["sub"], claims["account_id"])] = "admin"

    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200, r.text
    rotated = jwt_utils.verify_token(r.json()["access_token"])
    assert rotated["account_id"] == claims["account_id"]
    assert rotated["role"] == "admin"


def test_refresh_ends_the_session_when_membership_is_gone(client, accounts):
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)
    claims = jwt_utils.verify_token(tokens["access_token"])

    accounts["memberships"].pop((claims["sub"], claims["account_id"]))
    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401
    assert "No longer a member" in r.json()["detail"]
    assert not store.session_exists(claims["jti"])


def test_refresh_falls_back_to_the_stored_role_when_user_is_down(client, accounts, db_session):
    """Silent renewal must survive a User-service blip — a network failure is
    not evidence of a demotion, so the snapshot on the refresh row is used."""
    email = _unique_email()
    _signup_and_verify(client, email)
    tokens = _login(client, email)
    stored = db_session.scalars(select(RefreshToken)).all()
    assert [row.role for row in stored] == ["owner"]

    accounts["errors"]["get_membership"] = user_client.UserServiceError("down")
    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200, r.text
    assert jwt_utils.verify_token(r.json()["access_token"])["role"] == "owner"


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
