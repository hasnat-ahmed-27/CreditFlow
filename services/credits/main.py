"""
CreditFlow Credits/Marketplace service — owns the credit ledger: purchase
grants (from Billing's invoice.paid), AI generation debits (from the AI
service's ai.generation_completed), refund claw-backs, and peer-to-peer
buy/sell of credits between accounts.

One sentence of design: the balance is never stored, only DERIVED — every
change is an immutable credits_ledger row and the balance is their sum
(see models.py for the full argument).
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import marketplace
import routes

SERVICE_NAME = os.getenv("SERVICE_NAME", "credits")
# Tests set this to 0: pytest runs with no broker and calls consumer.handle_event directly.
CONSUMER_ENABLED = os.getenv("CREDITS_CONSUMER_ENABLED", "1") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create schema + tables on startup (spec-sanctioned simple path)
    if CONSUMER_ENABLED:
        import consumer
        # Daemon thread: pika's BlockingConnection would otherwise block the
        # event loop, and daemon=True lets uvicorn shut down cleanly. run()
        # fans out to one thread per queue (billing_events + usage_events +
        # account_events).
        threading.Thread(target=consumer.run, name="credits-consumer", daemon=True).start()
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
app.include_router(marketplace.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}
