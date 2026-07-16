"""
CreditFlow Usage/Metering service — enforces real-time quota checks and
tracks AI usage cost per account, per model.

One sentence of design: Redis holds a live per-period token counter for O(1)
quota pre-checks on the AI hot path, while the append-only Postgres
usage_ledger is the durable truth the counter is always rebuilt from
(see models.py for the full argument).
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import routes

SERVICE_NAME = os.getenv("SERVICE_NAME", "usage")
# Tests set this to 0: pytest runs with no broker and calls consumer.handle_event directly.
CONSUMER_ENABLED = os.getenv("USAGE_CONSUMER_ENABLED", "1") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create schema + tables on startup (spec-sanctioned simple path)
    if CONSUMER_ENABLED:
        import consumer
        # Daemon thread: pika's BlockingConnection would otherwise block the
        # event loop, and daemon=True lets uvicorn shut down cleanly. Starting
        # the consumer also DECLARES our durable usage.usage_events queue —
        # ready before the AI service (the publisher) even exists.
        threading.Thread(target=consumer.run, name="usage-consumer", daemon=True).start()
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
