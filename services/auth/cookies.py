"""
Refresh-token cookie transport (spec §4 Cross-Cutting: "store access token in
memory, refresh token in an httpOnly cookie; silent refresh on expiry").

WHY A COOKIE AT ALL
-------------------
The access token lives only in the frontend's JS memory, so it dies with the
tab and is never readable from storage by an XSS payload that runs later. That
only helps if the LONG-lived credential is also unreadable, which is what
httpOnly buys: script on the page cannot exfiltrate the refresh token, and a
page reload can still silently re-mint an access token by presenting the
cookie the browser attaches for us.

The trade is the one every cookie credential makes: it becomes AMBIENT — the
browser sends it on qualifying requests whether or not our code asked. That is
CSRF, and it is defended twice here:

  1. SameSite (default `strict`) — the browser refuses to attach the cookie to
     a request initiated by another site at all. Note that "site" ignores the
     port, so the dev frontend (localhost:5173) and the gateway
     (localhost:8080) are same-site and the cookie flows normally in dev.
  2. Double-submit token — a SECOND, deliberately non-httpOnly cookie carries
     a random value that our own JS reads and echoes back in `X-CSRF-Token`.
     A cross-site attacker can drive the browser into SENDING cookies but the
     same-origin policy stops them READING one, so they cannot produce the
     matching header.

Layer 2 exists because layer 1 is a browser policy we don't control: it is
weakened by same-site subdomain attackers and by any future deployment that
has to relax SameSite to `lax`/`none`.

The CSRF check applies ONLY to cookie-authenticated calls. A request that
carries the refresh token in its JSON body is not using ambient authority —
possession of that token is itself the proof — so it needs no second factor,
which is also why non-browser clients keep working unchanged.

COOKIE SCOPES
-------------
  refresh -> Path=/auth, httpOnly. Narrow on purpose: the browser attaches it
             to the two routes that consume it (refresh, logout) and to
             nothing else, so it never rides along on ordinary API traffic.
  csrf    -> Path=/, readable. Must be visible to the frontend's JS on its own
             origin, which means it cannot be confined to /auth.

Cookies are keyed by host and ignore the port, so a cookie the gateway sets on
localhost:8080 is readable by the frontend page on localhost:5173.
"""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Response

from creditflow_common import config

REFRESH_COOKIE = config.env("AUTH_REFRESH_COOKIE_NAME", "cf_refresh")
CSRF_COOKIE = config.env("AUTH_CSRF_COOKIE_NAME", "cf_csrf")
CSRF_HEADER = "x-csrf-token"

# Confined to the routes that consume it (see COOKIE SCOPES above).
REFRESH_PATH = config.env("AUTH_REFRESH_COOKIE_PATH", "/auth")

# Secure defaults to off so the flow works over plain http://localhost in dev;
# every real deployment sets AUTH_COOKIE_SECURE=1 (compose does).
SECURE = config.env("AUTH_COOKIE_SECURE", "0") == "1"
SAMESITE = config.env("AUTH_COOKIE_SAMESITE", "strict").lower()
DOMAIN = config.env("AUTH_COOKIE_DOMAIN", "") or None

# Matches the refresh token's own lifetime — an expired cookie and an expired
# token should stop working at the same moment.
MAX_AGE = config.JWT_REFRESH_TTL_SECONDS


def issue(response: Response, refresh_token: str) -> None:
    """Attach a rotated refresh cookie + a fresh CSRF token to `response`.

    Called on every mint path (login, refresh, switch-account) so the cookie
    tracks rotation: the token the browser holds is always the current one.
    """
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=MAX_AGE,
        path=REFRESH_PATH,
        domain=DOMAIN,
        secure=SECURE,
        httponly=True,
        samesite=SAMESITE,
    )
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        max_age=MAX_AGE,
        path="/",
        domain=DOMAIN,
        secure=SECURE,
        httponly=False,  # deliberately readable — that is the whole mechanism
        samesite=SAMESITE,
    )


def clear(response: Response) -> None:
    """Drop both cookies. Path/domain must match what `issue` set or the
    browser keeps the original cookie and logout silently fails to log out."""
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH, domain=DOMAIN)
    response.delete_cookie(CSRF_COOKIE, path="/", domain=DOMAIN)


def read_refresh(request: Request) -> str | None:
    return request.cookies.get(REFRESH_COOKIE)


def verify_csrf(request: Request) -> None:
    """Double-submit check for a cookie-authenticated call. Compared in
    constant time — the value is a secret for exactly as long as the session."""
    cookie_value = request.cookies.get(CSRF_COOKIE)
    header_value = request.headers.get(CSRF_HEADER)
    if not cookie_value or not header_value:
        raise HTTPException(status_code=403, detail="Missing CSRF token")
    if not secrets.compare_digest(cookie_value, header_value):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")


def resolve_refresh_token(request: Request, body_token: str | None) -> str:
    """Pick the credential for a refresh/logout call and enforce the rules
    that go with the one chosen.

    An explicit body token WINS over the cookie. That ordering matters: it
    keeps API/CLI clients (and the existing test suite) working untouched, and
    it means a browser that sends both is asking for the body one deliberately
    — the cookie is only ever the fallback, never an override of an explicit
    choice. Only the cookie path is ambient, so only it is CSRF-checked.
    """
    if body_token:
        return body_token
    cookie_token = read_refresh(request)
    if not cookie_token:
        raise HTTPException(status_code=401, detail="No refresh token supplied")
    verify_csrf(request)
    return cookie_token
