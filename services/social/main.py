"""
CreditFlow Social Publishing service — the service that actually pushes
content OUT: it holds each account's LinkedIn OAuth connection (tokens
Fernet-encrypted at rest) and publishes posts — text-only, or text+image via
LinkedIn's register-upload -> PUT binary -> asset-URN flow (the spec's
required bonus). Publishes happen two ways: the manual POST /publish route,
and the consumer draining the `social.scheduler_events` queue the Scheduler
pre-declared — when a schedule fires content.scheduled, this service posts
it (the Scheduler never calls LinkedIn itself).

One sentence of design: LinkedIn I/O lives in exactly one module
(linkedin.py) and every concluded attempt is a terminal publish_jobs row
committed BEFORE post.published/post.failed is emitted — so the audit
trail, the event stream, and the idempotent consumer (processed_events)
always agree, and a redelivered fire event can never double-post.
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import routes

SERVICE_NAME = os.getenv("SERVICE_NAME", "social")
# Tests set this to 0: pytest runs with no broker and calls consumer.handle_event directly.
CONSUMER_ENABLED = os.getenv("SOCIAL_CONSUMER_ENABLED", "1") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create schema + tables on startup (spec-sanctioned simple path)
    if CONSUMER_ENABLED:
        import consumer
        # Daemon thread: pika's BlockingConnection would otherwise block the
        # event loop, and daemon=True lets uvicorn shut down cleanly.
        threading.Thread(target=consumer.run, name="social-consumer", daemon=True).start()
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
