"""
CreditFlow Notification service — the central place for all outbound email:
signup verification and password resets (Auth events), tenant invites and
member welcomes (User events), receipts and dunning (Billing events), quota
and low-balance alerts (Usage/Credits events), and publish status emails
(Social events). Mostly a consumer with a minimal REST surface, per spec.

One sentence of design: provider I/O lives in exactly one module (mailer.py
— Resend primary, Mailgun fallback) and every concluded send is a terminal
notification_log row committed BEFORE notification.sent is emitted — so the
audit trail, the event stream, and the idempotent consumer
(processed_events) always agree, and a redelivered event can never
double-send an email.
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import routes

SERVICE_NAME = os.getenv("SERVICE_NAME", "notification")
# Tests set this to 0: pytest runs with no broker and calls consumer.handle_event directly.
CONSUMER_ENABLED = os.getenv("NOTIFICATION_CONSUMER_ENABLED", "1") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create schema + tables on startup (spec-sanctioned simple path)
    if CONSUMER_ENABLED:
        import consumer
        # Daemon thread: pika's BlockingConnection would otherwise block the
        # event loop, and daemon=True lets uvicorn shut down cleanly.
        # consumer.run() fans out into one thread per consumed queue.
        threading.Thread(target=consumer.run, name="notification-consumer", daemon=True).start()
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
