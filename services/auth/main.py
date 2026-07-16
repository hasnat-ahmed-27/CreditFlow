"""
CreditFlow Auth service — identity: signup, email verification, login,
JWT issuance/refresh (RS256), session revocation, password reset.

Only this service holds the RS256 PRIVATE key; every other service verifies
tokens with the public key alone (no shared secret to leak, and a Gateway
can't mint tokens — only check them).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import routes

SERVICE_NAME = os.getenv("SERVICE_NAME", "auth")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create schema + tables on startup (spec-sanctioned simple path)
    yield


app = FastAPI(title=f"CreditFlow — {SERVICE_NAME}", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/me")
def me(claims: dict = Depends(routes.current_claims)) -> dict:
    """Introspect the presented access token (also proves revocation works:
    after /auth/logout the same token gets a 401 here)."""
    return {
        "user_id": claims.get("sub"),
        "account_id": claims.get("account_id"),
        "role": claims.get("role"),
        "jti": claims.get("jti"),
    }
