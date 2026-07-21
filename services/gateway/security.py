"""
Gateway authentication + authorization (spec §8 Service 1: "Verify JWT on
every protected route"; §6: "Roles: Owner, Admin, Member — enforced at the
Gateway on every protected route").

The gateway verifies the RS256 access token with the SHARED PUBLIC KEY —
the same key every other service holds, via the same
`creditflow_common.jwt_utils.verify_token`. It still cannot mint anything:
only Auth holds the private key. The original `Authorization` header is
forwarded downstream untouched (see proxy.py), so every service keeps its
own verification — the gateway is the first line, never the only one.

PUBLIC ROUTES (no token required) are an explicit ALLOW-LIST, not a pattern:
anything not named here and owned by the route table is protected. The list
is exactly the routes that cannot have a token yet (or must not need one):
signup/login/verify-email/password-reset, `/auth/refresh` (it presents a
REFRESH token, which is not an access token and would fail verification
here), the provider webhooks (authenticated by signature instead — see
signatures.py), and health.

ROLE POLICY — the rule that keeps this from breaking working flows: the
gateway enforces exactly the gates the owning service derives FROM THE TOKEN
CLAIM, and nothing more. Where a service authorizes against data the gateway
does not hold, the gateway's policy stays at "any authenticated role" and
the service remains the authority:

  enforced here (claim-based, mirrored 1:1 from the service):
    /admin/*                               owner|admin|superadmin  (admin/routes.py admin_claims)
    /billing/{checkout,plan,refunds,invoices}   owner              (billing/routes.py require_owner)
    /credits/marketplace/listings POST|DELETE   owner              (credits/marketplace.py require_owner)
    POST /content/{id}/status              owner|admin             (content/routes.py PUBLISH_ROLES)
    /schedules POST|PATCH|DELETE           owner|admin             (scheduler/routes.py PUBLISH_ROLES)
    /connections POST|DELETE               owner|admin             (social/routes.py PUBLISH_ROLES)
    POST /publish                          owner|admin             (social/routes.py PUBLISH_ROLES)

  deliberately NOT enforced here:
    /accounts/*, /invites/*   — the User service authorizes against the
      account_members TABLE for the account named in the PATH, which may
      differ from the token's account_id. A claim-based gate here would be
      STRICTER than the service and would reject legitimate calls (an admin
      of account B holding an A-scoped token). Membership gates stay where
      the membership table is.
    DELETE /content/{id}      — the service's role gate is conditional on the
      item's status (drafts are any member's to delete). The gateway cannot
      see status, so it must not guess.

SuperAdmin does NOT bypass the owner/admin gates — mirroring the services,
which check `role == "owner"` literally. A SuperAdmin's token carries
`role: superadmin` (auth/superadmin.py role_for), so it is admitted to the
admin console and refused at the owner-only money routes, exactly as it is
downstream today.

Ordering note: a role rejection is decided before the request leaves the
gateway, so a member hitting `POST /content/{id}/status` gets 403 for EVERY
id — existing or not — and therefore leaks no existence, which is the
property the services' "tenant 404 before role 403" ordering protects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from creditflow_common import jwt_utils

# --- Roles (spec §6 + the platform SuperAdmin) -----------------------------
OWNER = "owner"
ADMIN = "admin"
MEMBER = "member"
SUPERADMIN = "superadmin"

OWNER_ONLY = (OWNER,)
MANAGER_ROLES = (OWNER, ADMIN)                     # the account-scoped managers
ADMIN_CONSOLE_ROLES = (OWNER, ADMIN, SUPERADMIN)   # admin/routes.py ADMIN_ROLES

# --- Public routes (spec: login, signup, verify-email, forgot-password,
# webhooks, health stay unauthenticated) ------------------------------------
PUBLIC_PATHS = frozenset({
    "/health",
    "/health/upstreams",
    "/auth/signup",
    "/auth/login",
    "/auth/verify-email",
    "/auth/refresh",                 # presents a REFRESH token, not an access one
    "/auth/password-reset/request",  # "forgot password"
    "/auth/password-reset/confirm",
})

# Provider webhooks authenticate by signature, not by bearer (webhooks.py).
PUBLIC_PREFIXES = ("/webhooks/",)

ANY_METHOD = "*"


@dataclass(frozen=True)
class RoleRule:
    """One row of the policy table. `why` names the downstream gate this
    mirrors, so the two can never drift silently."""
    methods: tuple[str, ...]
    pattern: re.Pattern[str]
    roles: tuple[str, ...]
    why: str


def _rule(methods: tuple[str, ...], pattern: str, roles: tuple[str, ...], why: str) -> RoleRule:
    return RoleRule(methods, re.compile(pattern), roles, why)


# First match wins; anything unmatched needs only a valid access token.
ROLE_RULES: tuple[RoleRule, ...] = (
    _rule(
        (ANY_METHOD,), r"^/admin(?:/.*)?$", ADMIN_CONSOLE_ROLES,
        "admin/routes.py admin_claims — the console has no non-admin surface",
    ),
    _rule(
        (ANY_METHOD,), r"^/billing/(?:checkout|plan|refunds|invoices)(?:/.*)?$", OWNER_ONLY,
        "billing/routes.py require_owner — subscription money movement",
    ),
    _rule(
        ("POST", "DELETE"), r"^/credits/marketplace/listings(?:/.*)?$", OWNER_ONLY,
        "credits/marketplace.py require_owner — selling/buying account value",
    ),
    _rule(
        ("POST",), r"^/content/[^/]+/status$", MANAGER_ROLES,
        "content/routes.py _require_publish_role — lifecycle transitions",
    ),
    _rule(
        ("POST", "PATCH", "DELETE"), r"^/schedules(?:/.*)?$", MANAGER_ROLES,
        "scheduler/routes.py _require_publish_role — calendar mutations",
    ),
    _rule(
        ("POST", "DELETE"), r"^/connections(?:/.*)?$", MANAGER_ROLES,
        "social/routes.py _require_publish_role — connect/disconnect LinkedIn",
    ),
    _rule(
        ("POST",), r"^/publish$", MANAGER_ROLES,
        "social/routes.py _require_publish_role — the outward-facing act",
    ),
)


class AuthError(Exception):
    """A gateway-decided rejection, carried to the middleware as the
    consistent {status, detail} the error schema renders."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def normalize(path: str) -> str:
    """Trailing slashes are not a different route ('/schedules/' must not
    dodge the calendar rule)."""
    return path.rstrip("/") or "/"


def is_public(path: str) -> bool:
    path = normalize(path)
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def required_roles(method: str, path: str) -> tuple[str, ...] | None:
    """The roles admitted to this route, or None when any authenticated
    role will do."""
    path = normalize(path)
    method = method.upper()
    for rule in ROLE_RULES:
        if ANY_METHOD in rule.methods or method in rule.methods:
            if rule.pattern.match(path):
                return rule.roles
    return None


def verify(authorization: str) -> dict:
    """Verify the Bearer access token's RS256 signature + expiry. Same checks
    and same wording as every service's `current_claims`, so a client sees
    one error vocabulary whether the gateway or the service refused."""
    if not authorization.lower().startswith("bearer "):
        raise AuthError(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = jwt_utils.verify_token(token)
    except jwt_utils.TokenError as exc:
        raise AuthError(401, str(exc)) from exc
    if claims.get("type") != "access":
        raise AuthError(401, "Not an access token")
    if not claims.get("account_id"):
        # Every access token is account-scoped (spec §6). One without a scope
        # cannot be authorized or rate-limited per account.
        raise AuthError(401, "Token is missing account scope")
    return claims


def authorize(method: str, path: str, claims: dict) -> None:
    roles = required_roles(method, path)
    if roles is None:
        return
    if claims.get("role") not in roles:
        raise AuthError(403, f"Requires one of these roles: {', '.join(roles)}")


def authenticate(method: str, path: str, authorization: str) -> dict:
    """Verify then authorize. Returns the claims for the caller to stash on
    request.state (the aggregation endpoints and the per-account rate limiter
    read them instead of parsing the token a second time)."""
    claims = verify(authorization)
    authorize(method, path, claims)
    return claims
