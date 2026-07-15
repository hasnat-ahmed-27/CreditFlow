"""
Celery wiring for the Scheduler service — Redis is both broker and result
backend (spec), on a SEPARATE logical database index (default /1) from the
caching/JTI Redis usage on /0, also per spec.

Three processes share this module (see docker-compose):
  - the FastAPI API (never runs tasks; schedules are picked up by beat),
  - `celery -A celery_app worker` — executes scan/fire tasks,
  - `celery -A celery_app beat`   — ticks tasks.scan_due_schedules every
    SCHEDULER_SCAN_INTERVAL seconds (default 60, the spec's example).

Tests set SCHEDULER_CELERY_EAGER=1 (see conftest): task_always_eager makes
every .delay() run inline in-process — no worker, no beat, no broker
connection is ever made — and task_eager_propagates surfaces task exceptions
as test failures instead of swallowed results.
"""
from __future__ import annotations

import os

from celery import Celery

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/1")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", BROKER_URL)
SCAN_INTERVAL_SECONDS = float(os.getenv("SCHEDULER_SCAN_INTERVAL", "60"))

celery = Celery("scheduler", broker=BROKER_URL, backend=RESULT_BACKEND)

celery.conf.update(
    imports=("tasks",),                 # worker/beat load the task module by name
    task_always_eager=os.getenv("SCHEDULER_CELERY_EAGER", "0") == "1",
    task_eager_propagates=True,
    timezone="UTC",                     # beat ticks in UTC, same as storage
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "scan-due-schedules": {
            "task": "tasks.scan_due_schedules",
            "schedule": SCAN_INTERVAL_SECONDS,
        },
    },
)
