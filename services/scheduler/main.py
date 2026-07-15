"""
CreditFlow Scheduler service — owns each account's content calendar: WHEN
content goes out. Users attach a future publish_at (one-off, or recurring on
a daily/weekly/monthly cadence — the spec's bonus) to existing content; a
Celery Beat scan finds due schedules and a worker task emits
content.scheduled for the Social Publishing service to act on — this service
never calls LinkedIn itself.

One sentence of design: Postgres rows (scheduled_posts) are the single source
of truth, the API only ever writes rows, and the Celery worker/beat pair
(separate processes, see celery_app.py and docker-compose) turns due rows
into events — so the API stays up if Celery is down, and firings survive
restarts because they are derived from rows, not in-memory timers. What is
schedulable is learned by consuming the content_events queue the Content
service pre-declared for us (see consumer.py for the contract).
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import routes

SERVICE_NAME = os.getenv("SERVICE_NAME", "scheduler")
# Tests set this to 0: pytest runs with no broker and calls consumer.handle_event directly.
CONSUMER_ENABLED = os.getenv("SCHEDULER_CONSUMER_ENABLED", "1") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create schema + tables on startup (spec-sanctioned simple path)
    if CONSUMER_ENABLED:
        import consumer
        # Daemon thread: pika's BlockingConnection would otherwise block the
        # event loop, and daemon=True lets uvicorn shut down cleanly.
        threading.Thread(target=consumer.run, name="scheduler-consumer", daemon=True).start()
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
