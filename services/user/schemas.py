"""Request payloads. Responses are plain dicts, documented in routes.py."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr, Field

# Invites and role updates can never mint an owner — ownership is established
# only at account creation (transfer would be a separate, deliberate flow).
GrantableRole = Literal["admin", "member"]


class CreateTeamRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class UpdateAccountRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class InviteRequest(BaseModel):
    email: EmailStr
    role: GrantableRole = "member"


class AcceptInviteRequest(BaseModel):
    token: str


class UpdateMemberRoleRequest(BaseModel):
    role: GrantableRole


class EnsureIndividualAccountRequest(BaseModel):
    """Auth's signup/login provisioning call (internal.py). `email` only names
    the account for display — identity is the Auth-owned user_id."""
    user_id: str = Field(min_length=1, max_length=36)
    email: str | None = Field(default=None, max_length=255)
