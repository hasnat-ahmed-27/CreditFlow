"""
CreditFlow Billing service — owns all Stripe (test-mode) interaction:
customers, checkout/subscriptions, proration, refunds, dunning — and the
spec §7 Transactional Outbox that makes its events loss-proof.

Reliability in one sentence: webhooks are persisted before they are
processed, every state change commits atomically with its outbox event row,
and a background poller drains the outbox to RabbitMQ — so a crash or a
broker outage can delay events but never lose them.
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import routes
import stripe_gateway
import webhooks

SERVICE_NAME = os.getenv("SERVICE_NAME", "billing")
# Tests set this to 0: pytest drives outbox.publish_pending / dunning.apply_due
# directly. It can also be 0 in deployments that run `python -m poller` as a
# separate process instead of an in-service thread.
POLLER_ENABLED = os.getenv("BILLING_POLLER_ENABLED", "1") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    stripe_gateway.init()  # refuses to boot on a non-test Stripe key
    database.init_db()     # create schema + tables on startup (spec-sanctioned simple path)
    if POLLER_ENABLED:
        import poller
        # Daemon thread, same pattern as the user service's consumer: the
        # blocking poll loop must not sit on the event loop, and daemon=True
        # lets uvicorn shut down cleanly.
        threading.Thread(target=poller.run, name="billing-outbox-poller", daemon=True).start()
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
app.include_router(webhooks.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}
