"""
Base FastAPI service template. Copy this folder to services/<name> to start a
new CreditFlow service, then implement its routes and event handlers.

What's wired already:
  - /health   liveness
  - /me       example of RS256 JWT verification via the shared lib
"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Shared cross-cutting helpers (installed as the `creditflow_common` package).
from creditflow_common import jwt_utils

SERVICE_NAME = os.getenv("SERVICE_NAME", "template")

app = FastAPI(title=f"CreditFlow — {SERVICE_NAME}", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def current_claims(authorization: str = Header(default="")) -> dict:
    """Verify the Bearer token and return its claims (user_id, account_id, role, jti)."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        return jwt_utils.verify_token(token)
    except jwt_utils.TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/me")
def me(claims: dict = Depends(current_claims)) -> dict:
    return {
        "user_id": claims.get("sub"),
        "account_id": claims.get("account_id"),
        "role": claims.get("role"),
        "jti": claims.get("jti"),
    }
