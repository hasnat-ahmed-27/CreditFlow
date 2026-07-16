"""
Celery wiring for the Scraper service — Redis is both broker and result
backend, on its OWN logical database index (default /2; /1 belongs to the
Scheduler's Celery, /0 to caching/JTI — one index per concern, same rule).

Three processes share this module (see docker-compose):
  - the FastAPI API — dispatches run_scrape for one-off jobs and never
    executes scrapes itself,
  - `celery -A celery_app worker` — executes scan/scrape tasks (this is the
    spec's "worker process for execution", and the only container that
    launches Chromium),
  - `celery -A celery_app beat` — the spec's "internal scheduler" for
    recurring scrape jobs: ticks tasks.scan_due_scrapes every
    SCRAPER_SCAN_INTERVAL seconds (default 60).

Tests set SCRAPER_CELERY_EAGER=1 (see conftest): task_always_eager makes
every .delay() run inline in-process — no worker, no beat, no broker
connection is ever made — and task_eager_propagates surfaces task exceptions
as test failures instead of swallowed results.
"""
from __future__ import annotations

import os

from celery import Celery

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/2")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", BROKER_URL)
SCAN_INTERVAL_SECONDS = float(os.getenv("SCRAPER_SCAN_INTERVAL", "60"))

celery = Celery("scraper", broker=BROKER_URL, backend=RESULT_BACKEND)

celery.conf.update(
    imports=("tasks",),                 # worker/beat load the task module by name
    task_always_eager=os.getenv("SCRAPER_CELERY_EAGER", "0") == "1",
    task_eager_propagates=True,
    timezone="UTC",                     # beat ticks in UTC, same as storage
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "scan-due-scrapes": {
            "task": "tasks.scan_due_scrapes",
            "schedule": SCAN_INTERVAL_SECONDS,
        },
    },
)
