"""
CreditFlow Scraper service — runs web-scraping jobs (trend/competitor data)
on demand or on schedule, feeding raw source material for content
generation. Jobs arrive two ways: the authenticated REST API (POST
/scrape-jobs) and the `scrape.requested` broker contract (see consumer.py);
both funnel into the same Celery run_scrape task, executed by the worker
container — the only place a headless browser ever launches (see
scraper_engine.py). Recurring jobs (daily competitor checks) are re-fired by
Celery beat, the spec's "internal scheduler".

One sentence of design: this is the stack's one MongoDB service (raw pages
have no fixed shape) and every Mongo read/write goes through store.py
tenant-scoped by account_id, while the processed_events idempotency ledger
stays on the shared Postgres (database.py) like every other consumer — the
document store holds the data, the relational store holds the invariant.
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import routes
import store

SERVICE_NAME = os.getenv("SERVICE_NAME", "scraper")
# Tests set this to 0: pytest runs with no broker and calls consumer.handle_event directly.
CONSUMER_ENABLED = os.getenv("SCRAPER_CONSUMER_ENABLED", "1") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # processed_events ledger (Postgres — see database.py)
    store.init()        # Mongo indexes (idempotent)
    if CONSUMER_ENABLED:
        import consumer
        # Daemon thread: pika's BlockingConnection would otherwise block the
        # event loop, and daemon=True lets uvicorn shut down cleanly.
        threading.Thread(target=consumer.run, name="scraper-consumer", daemon=True).start()
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
