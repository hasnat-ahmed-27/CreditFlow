"""
Auth endpoints.

Token model (RS256, keys shared via /keys volume):
  - access token: short-lived; payload sub/account_id/role/jti; its jti is
    stored in Redis (TTL = expiry) so it can be revoked before natural expiry.
  - refresh token: long-lived; one DB row per token; ROTATED on every use —
    the old row is revoked and points at its replacement. Presenting an
    already-rotated refresh token is treated as theft: every active session
    for that user is revoked.

Claims (spec §6 — user_id, account_id, role, jti): account_id and role are
RESOLVED FROM THE USER SERVICE, which owns accounts and account_members;
see user_client.py for why that call is synchronous rather than an event-fed
read model. Signup provisions the individual account, login and account-
switch mint tokens scoped to a real account, and refresh re-resolves the role
so a demotion takes effect on the next rotation.

A designated platform SuperAdmin (superadmin.py) gets `role: "superadmin"`
instead of their account role — platform-level, not account-scoped.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from creditflow_common import config, jwt_utils

import database
import events
import schemas
import security
import store
import superadmin
import user_client
from models import Credential, PasswordResetToken, RefreshToken, User, as_aware, utcnow

logger = logging.getLogger("auth.routes")

router = APIRouter(prefix="/auth", tags=["auth"])

# TTLs / limits (env-overridable; sane defaults per spec: short-lived + single-use).
VERIFICATION_TTL_SECONDS = int(config.env("AUTH_VERIFICATION_TTL_SECONDS", "3600"))
RESET_TTL_SECONDS = int(config.env("AUTH_RESET_TTL_SECONDS", "900"))
LOGIN_MAX_ATTEMPTS = int(config.env("AUTH_LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = int(config.env("AUTH_LOGIN_WINDOW_SECONDS", "60"))
# Dev convenience: echo one-time tokens in responses so the flow can be tested
# with curl before the Notification service exists. NEVER enable in prod.
EXPOSE_DEV_TOKENS = config.env("AUTH_EXPOSE_DEV_TOKENS", "0") == "1"


def current_claims(authorization: str = Header(default="")) -> dict:
    """Verify Bearer access token: RS256 signature + jti still active in Redis."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        claims = jwt_utils.verify_token(token)
    except jwt_utils.TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if claims.get("type") != "access":
        raise HTTPException(status_code=401, detail="Not an access token")
    if not store.session_exists(claims["jti"]):
        raise HTTPException(status_code=401, detail="Session revoked or expired")
    return claims


def _issue_token_pair(db: Session, user: User, account_id: str, role: str) -> tuple[dict, str, str]:
    """Sign access+refresh scoped to `account_id`, store the access jti in
    Redis, persist the refresh row. Returns (response payload, access_jti,
    refresh_jti). Caller commits.

    `role` is the ACCOUNT role the User service resolved; the platform
    SuperAdmin flag overrides it here so every mint path — login, switch,
    refresh — goes through one rule."""
    role = superadmin.role_for(user, role)

    access_token, access_jti = jwt_utils.sign_access_token(user.id, account_id, role)
    refresh_token, refresh_jti = jwt_utils.sign_refresh_token(user.id, account_id)

    store.store_session(access_jti, user.id, account_id, role, config.JWT_ACCESS_TTL_SECONDS)
    db.add(RefreshToken(
        jti=refresh_jti,
        user_id=user.id,
        account_id=account_id,
        role=role,
        access_jti=access_jti,
        expires_at=utcnow() + timedelta(seconds=config.JWT_REFRESH_TTL_SECONDS),
    ))
    payload = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": config.JWT_ACCESS_TTL_SECONDS,
        "account_id": account_id,
        "role": role,
    }
    return payload, access_jti, refresh_jti


def _resolve_default_scope(user: User) -> tuple[str, str]:
    """(account_id, role) for the user's own individual account, provisioning
    it if the signup-time call or the `user.registered` consumer never landed.

    Fails CLOSED with 503: minting a token with a guessed account_id would
    scope every downstream service's data to the wrong tenant."""
    try:
        account = user_client.ensure_individual_account(user.id, user.email)
    except user_client.UserServiceError as exc:
        logger.error("cannot resolve account for %s: %s", user.id, exc)
        raise HTTPException(
            status_code=503,
            detail="Account service unavailable — cannot issue an account-scoped token",
        )
    return account["account_id"], account["role"]


def _revoke_all_user_tokens(db: Session, user_id: str) -> None:
    """Kill every active refresh token AND its paired access session."""
    rows = db.scalars(
        select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
    ).all()
    for row in rows:
        row.revoked_at = utcnow()
        if row.access_jti:
            store.revoke_session(row.access_jti)


# --------------------------------------------------------------------------
# Signup + email verification
# --------------------------------------------------------------------------

@router.post("/signup", status_code=201)
def signup(body: schemas.SignupRequest, db: Session = Depends(database.get_db)) -> dict:
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="Email already registered")

    verification_token = security.new_one_time_token()
    user = User(
        email=email,
        verification_token_hash=security.hash_token(verification_token),
        verification_expires_at=utcnow() + timedelta(seconds=VERIFICATION_TTL_SECONDS),
        # Designated operators are super from their first token, without
        # waiting for the next startup reconcile (superadmin.py).
        is_superadmin=superadmin.is_designated(email),
    )
    db.add(user)
    db.flush()  # assigns user.id for the FK below
    db.add(Credential(user_id=user.id, password_hash=security.hash_password(body.password)))
    db.commit()

    # Spec §8 Service 3: "create an Account automatically on signup (type:
    # individual)". Best-effort — the User service's `user.registered`
    # consumer provisions the same account idempotently, so an outage here
    # only delays it (and login re-tries the call before minting anyway).
    account_id = None
    try:
        account_id = user_client.ensure_individual_account(user.id, email)["account_id"]
    except user_client.UserServiceError as exc:
        logger.warning("signup %s: individual account not provisioned synchronously (%s) — "
                       "the user.registered consumer will create it", user.id, exc)

    # Notification service consumes this and emails the verification link;
    # the User service consumes it as the asynchronous provisioning path.
    events.publish("user.registered", {
        "user_id": user.id,
        "email": email,
        "verification_token": verification_token,
    })

    resp = {"user_id": user.id, "email": email, "account_id": account_id,
            "message": "Verification email queued"}
    if EXPOSE_DEV_TOKENS:
        resp["dev_verification_token"] = verification_token
    return resp


@router.post("/verify-email")
def verify_email(body: schemas.VerifyEmailRequest, db: Session = Depends(database.get_db)) -> dict:
    user = db.scalar(select(User).where(User.verification_token_hash == security.hash_token(body.token)))
    if user is None or as_aware(user.verification_expires_at) < utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")
    user.is_verified = True
    user.verification_token_hash = None  # single-use
    user.verification_expires_at = None
    db.commit()
    return {"message": "Email verified — you can log in now"}


# --------------------------------------------------------------------------
# Login / refresh / logout
# --------------------------------------------------------------------------

@router.post("/login")
def login(body: schemas.LoginRequest, request: Request, db: Session = Depends(database.get_db)) -> dict:
    email = body.email.lower()
    ip = request.client.host if request.client else "unknown"
    # Per-email and per-IP sliding windows (IP gets more headroom: several
    # legit users can share a NAT/proxy address).
    if store.rate_limit_exceeded(f"login:email:{email}", LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS) \
            or store.rate_limit_exceeded(f"login:ip:{ip}", LOGIN_MAX_ATTEMPTS * 4, LOGIN_WINDOW_SECONDS):
        raise HTTPException(status_code=429, detail="Too many login attempts, try again later")

    user = db.scalar(select(User).where(User.email == email))
    cred = db.get(Credential, user.id) if user else None
    # Same message whether email or password was wrong — don't leak which.
    if cred is None or not security.verify_password(body.password, cred.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_verified:
        raise HTTPException(status_code=403, detail="Email not verified")

    # Real claims: the User service says which account this user owns and
    # with what role (503 if it can't answer — see _resolve_default_scope).
    account_id, role = _resolve_default_scope(user)
    payload, access_jti, _ = _issue_token_pair(db, user, account_id, role)
    db.commit()
    events.publish("user.logged_in", {"user_id": user.id, "email": email, "jti": access_jti})
    return payload


@router.post("/refresh")
def refresh(body: schemas.RefreshRequest, db: Session = Depends(database.get_db)) -> dict:
    try:
        claims = jwt_utils.verify_token(body.refresh_token)
    except jwt_utils.TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if claims.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")

    row = db.get(RefreshToken, claims["jti"])
    if row is None:
        raise HTTPException(status_code=401, detail="Unknown refresh token")
    if row.revoked_at is not None:
        # Rotation reuse = the token was stolen (or the client is broken).
        # Nuke every session for this user and force a fresh login.
        _revoke_all_user_tokens(db, row.user_id)
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh token reuse detected — all sessions revoked")
    if as_aware(row.expires_at) < utcnow():
        raise HTTPException(status_code=401, detail="Refresh token expired")

    user = db.get(User, row.user_id)
    # Re-resolve the role for the scope this token carries, so a demotion or
    # removal takes effect on the next rotation instead of riding out the
    # refresh token's 14-day life. A User-service outage falls back to the
    # role snapshot on the row (refresh is the frontend's silent-renewal
    # path — it must survive a blip); a definite "not a member" ends it.
    role = row.role
    try:
        membership = user_client.get_membership(row.user_id, row.account_id)
    except user_client.UserServiceError as exc:
        logger.warning("refresh %s: user service unreachable (%s) — reusing stored role %r",
                       row.jti, exc, role)
    else:
        if membership is None:
            _revoke_all_user_tokens(db, row.user_id)
            db.commit()
            raise HTTPException(status_code=401,
                                detail="No longer a member of this account — log in again")
        role = membership["role"]

    # Rotate: revoke the old pair (refresh row + its access session), link the
    # replacement for auditability, issue a fresh pair.
    row.revoked_at = utcnow()
    if row.access_jti:
        store.revoke_session(row.access_jti)
    payload, _, new_refresh_jti = _issue_token_pair(db, user, row.account_id, role)
    row.replaced_by_jti = new_refresh_jti
    db.commit()
    return payload


@router.post("/switch-account")
def switch_account(
    body: schemas.SwitchAccountRequest,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    """Spec §4: "Account Switcher … lets a user move between every account
    they belong to; triggers a new account-scoped JWT."

    Membership is checked against the User service, never against the
    presented token — the caller could otherwise name any account_id and get
    a token for it. A non-member is refused with 403 whether the account is
    foreign or nonexistent (the User service answers 404 for both, so ids
    leak nothing either way).

    The presenting access session is revoked as part of the switch: leaving
    the old account's token live would mean one login holding two concurrent
    scopes, which is precisely what account scoping exists to prevent. The
    old refresh token stays valid only until its own rotation, at which point
    it re-resolves against its own account.

    A platform SuperAdmin gets a superadmin-role token here too, but still
    only for accounts they actually belong to: the platform role grants
    cross-account VISIBILITY through the Admin service, not membership."""
    user = db.get(User, claims["sub"])
    if user is None:
        raise HTTPException(status_code=401, detail="Unknown user")

    try:
        membership = user_client.get_membership(user.id, body.account_id)
    except user_client.UserServiceError as exc:
        logger.error("switch-account %s -> %s: %s", user.id, body.account_id, exc)
        raise HTTPException(status_code=503, detail="Account service unavailable")
    if membership is None:
        raise HTTPException(status_code=403, detail="Not a member of this account")

    payload, _, _ = _issue_token_pair(db, user, membership["account_id"], membership["role"])
    store.revoke_session(claims["jti"])  # the previous scope stops working now
    db.commit()
    return payload


@router.post("/logout")
def logout(
    body: schemas.LogoutRequest | None = None,
    claims: dict = Depends(current_claims),
    db: Session = Depends(database.get_db),
) -> dict:
    # Deleting the jti from Redis invalidates the access token platform-wide,
    # immediately — every service checks Redis, not just the signature.
    store.revoke_session(claims["jti"])
    if body is not None and body.refresh_token:
        try:
            refresh_claims = jwt_utils.verify_token(body.refresh_token)
            row = db.get(RefreshToken, refresh_claims.get("jti", ""))
            if row is not None and row.user_id == claims["sub"] and row.revoked_at is None:
                row.revoked_at = utcnow()
                db.commit()
        except jwt_utils.TokenError:
            pass  # already unusable — nothing to revoke
    return {"message": "Logged out"}


# --------------------------------------------------------------------------
# Password reset
# --------------------------------------------------------------------------

@router.post("/password-reset/request", status_code=202)
def request_password_reset(
    body: schemas.PasswordResetRequest, db: Session = Depends(database.get_db)
) -> dict:
    email = body.email.lower()
    user = db.scalar(select(User).where(User.email == email))
    reset_token = None
    if user is not None:
        reset_token = security.new_one_time_token()
        db.add(PasswordResetToken(
            user_id=user.id,
            token_hash=security.hash_token(reset_token),
            expires_at=utcnow() + timedelta(seconds=RESET_TTL_SECONDS),
        ))
        db.commit()
        events.publish("user.password_reset_requested", {
            "user_id": user.id,
            "email": email,
            "reset_token": reset_token,
        })
    # Always 202 with the same body — don't reveal whether the email exists.
    resp = {"message": "If that email is registered, a reset link has been sent"}
    if EXPOSE_DEV_TOKENS and reset_token is not None:
        resp["dev_reset_token"] = reset_token
    return resp


@router.post("/password-reset/confirm")
def confirm_password_reset(
    body: schemas.PasswordResetConfirm, db: Session = Depends(database.get_db)
) -> dict:
    row = db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == security.hash_token(body.token))
    )
    if row is None or row.used_at is not None or as_aware(row.expires_at) < utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    cred = db.get(Credential, row.user_id)
    cred.password_hash = security.hash_password(body.new_password)
    row.used_at = utcnow()  # single-use
    # A password reset usually means the account was compromised — end every
    # existing session so only the new password grants access.
    _revoke_all_user_tokens(db, row.user_id)
    db.commit()
    return {"message": "Password updated — log in with your new password"}
