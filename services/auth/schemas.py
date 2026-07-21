"""Request payloads. Responses are plain dicts, documented in routes.py."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class VerifyEmailRequest(BaseModel):
    token: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    # Optional: also revoke the refresh token, not just the access session.
    refresh_token: str | None = None


class SwitchAccountRequest(BaseModel):
    # Membership is verified against the User service — naming an account here
    # is a request, not a claim.
    account_id: str = Field(min_length=1, max_length=36)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)
