"""
CreditFlow Content service — owns generated content (posts) as first-class
records: versioned drafts, the server-enforced draft -> approved ->
scheduled -> published lifecycle, and optional attached images.

One sentence of design: this service is the durable store the Scheduler and
Social Publishing services read from and advance — every mutation lands in
Postgres first and is then announced on the content_events exchange, and
AI-generated post drafts arrive by consuming the ai.generation_completed
queue the AI service pre-declared for us (see consumer.py for the contract).
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import routes

SERVICE_NAME = os.getenv("SERVICE_NAME", "content")
# Tests set this to 0: pytest runs with no broker and calls consumer.handle_event directly.
CONSUMER_ENABLED = os.getenv("CONTENT_CONSUMER_ENABLED", "1") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create schema + tables on startup (spec-sanctioned simple path)
    if CONSUMER_ENABLED:
        import consumer
        # Daemon thread: pika's BlockingConnection would otherwise block the
        # event loop, and daemon=True lets uvicorn shut down cleanly.
        threading.Thread(target=consumer.run, name="content-consumer", daemon=True).start()
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
