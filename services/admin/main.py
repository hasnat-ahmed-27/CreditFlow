"""
CreditFlow Admin/Ops service — the operational visibility layer (spec
service 13): active-session viewer + revocation (reading the same Redis jti
store Auth writes), the append-only audit_log built by consuming EVERY
domain event off every exchange, a cross-account directory with
suspend/reactivate oversight, and an aggregate per-account view pulled
read-only from the User/Credits/Usage services.

One sentence of design: this service VERIFIES and OBSERVES but never mints
or emits — public JWT key only, "Publishes: none" per spec — and every route
sits behind the admin-role gate (member tokens get 403 everywhere), with
SuperAdmin as the only cross-account tier.
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import routes

SERVICE_NAME = os.getenv("SERVICE_NAME", "admin")
# Tests set this to 0: pytest runs with no broker and calls consumer.handle_event directly.
CONSUMER_ENABLED = os.getenv("ADMIN_CONSUMER_ENABLED", "1") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create schema + tables on startup (spec-sanctioned simple path)
    if CONSUMER_ENABLED:
        import consumer
        # Daemon thread: pika's BlockingConnection would otherwise block the
        # event loop, and daemon=True lets uvicorn shut down cleanly.
        # consumer.run() fans out into one thread per consumed queue.
        threading.Thread(target=consumer.run, name="admin-consumer", daemon=True).start()
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
