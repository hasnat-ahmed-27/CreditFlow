"""
Response aggregation (spec §8 Service 1: "Aggregate/compose responses where a
frontend screen needs data from more than one service").

GET /dashboard/summary backs the Owner Dashboard's "account-wide summary:
usage, credits, team size, plan tier" (spec §4). That one screen needs three
services — User (account profile + seat count), Credits (balance), Usage
(this period) — which without this endpoint is three separate round trips the
browser fans out and stitches itself. Here the gateway makes them CONCURRENTLY
over its existing connection pool and returns one document.

SCOPING — no new authority. The request has already cleared the gateway's
JWT verification (main.py), so `request.state.claims` holds verified claims;
we forward the caller's OWN bearer to each upstream, exactly as the Admin
service's overview does (admin/clients.py). Each service then applies its own
account scoping to that token. The gateway invents no identity and reads no
data it could not already reach by proxying — it only saves the browser the
fan-out.

DEGRADE PER SECTION, never fail the whole view: a single upstream being down
lands its name in `degraded` and leaves that section null, so the dashboard
still renders everything that answered. Same contract, almost the same code,
as admin/routes.py account_overview — the difference is this one is
self-scoped (the caller's own account) and needs no admin role.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import proxy

logger = logging.getLogger("gateway.aggregate")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

AGGREGATE_TIMEOUT_SECONDS = 10.0


async def _fetch(request: Request, service: str, path: str) -> dict:
    """One read against an upstream, carrying the caller's bearer. Raises
    httpx.HTTPError (network/timeout) or HTTPStatusError (non-2xx) — the
    caller turns either into a degraded section."""
    client = proxy.get_client()
    headers = {}
    auth = request.headers.get("authorization")
    if auth:
        headers["authorization"] = auth
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        headers["x-request-id"] = request_id
    resp = await client.get(
        proxy.SERVICES[service] + path,
        headers=headers,
        timeout=httpx.Timeout(AGGREGATE_TIMEOUT_SECONDS),
    )
    resp.raise_for_status()
    return resp.json()


@router.get("/summary")
async def dashboard_summary(request: Request) -> JSONResponse:
    claims = getattr(request.state, "claims", None) or {}
    account_id = claims.get("account_id")

    sections = {
        "profile": ("user", f"/accounts/{account_id}"),
        "credits": ("credits", "/credits/balance"),
        "usage": ("usage", "/usage/summary"),
    }

    async def load(name: str, service: str, path: str):
        try:
            return name, await _fetch(request, service, path)
        except httpx.HTTPError as exc:
            logger.warning("dashboard summary: %s section degraded: %s", name, exc)
            return name, exc

    results = await asyncio.gather(
        *(load(name, service, path) for name, (service, path) in sections.items())
    )

    summary: dict = {
        "account_id": account_id,
        "profile": None,
        "credits": None,
        "usage": None,
        "degraded": [],
    }
    for name, result in results:
        if isinstance(result, Exception):
            summary["degraded"].append(sections[name][0])
        else:
            summary[name] = result

    return JSONResponse(summary)
