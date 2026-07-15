"""
Celery tasks — the due-time firing loop.

scan_due_schedules (beat, every SCHEDULER_SCAN_INTERVAL seconds): one cheap
query for pending schedules whose publish_at has passed, then one
fire_schedule task PER due item — so a slow/failed fire never blocks the
scan, and each firing carries its own double-fire protection.

fire_schedule(schedule_id, due_at_iso): emits the `content.scheduled` event
(see events.py for the contract) and advances the row — one-off schedules
become `fired`; recurring ones re-arm publish_at to the next occurrence in
their own timezone (recurrence.next_occurrence) and stay `pending`.

Double-fire protection (spec: "Prevent double-firing a schedule if the beat
task overlaps — use a Redis lock or Celery task idempotency key per
scheduled_post id"), two independent layers:
  1. A Redis SET NX lock keyed on (schedule_id, due occurrence) — two workers
     that pick up the same firing race on one atomic SETNX. Redis being down
     degrades to layer 2 rather than blocking publishes.
  2. The DB re-check inside the task: the row must still be `pending` AND its
     publish_at must still equal the occurrence the scan saw. A previous
     firing either flipped the status (one-off) or advanced publish_at
     (recurring), so a duplicate/stale task is a no-op either way.

Ordering follows the repo rule — commit FIRST, then publish (never announce
a write that didn't land). The tradeoff: a broker outage at exactly fire time
loses that occurrence's event (logged by events.publish); the committed
fire_count/last_fired_at keep the calendar truthful about the attempt.

If the content_refs read model no longer has the schedule's content (the
content was deleted and consumer.py's cancel raced past this firing), the
schedule is cancelled instead of firing an event pointing at nothing.

Tasks return a short status string ("fired" / "duplicate" / "stale" / ...) —
it lands in the Celery result backend, which makes production debugging via
`celery result` and test assertions equally direct.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from creditflow_common import config

import database
import events
import recurrence
from celery_app import celery
from models import ContentRef, ScheduledPost, as_utc, utcnow

logger = logging.getLogger("scheduler.tasks")

FIRE_LOCK_TTL_SECONDS = 600  # generous vs. the 60s scan — covers slow workers

_redis = None


def get_redis():
    """Lazy sync Redis client for the fire lock (tests monkeypatch this)."""
    global _redis
    if _redis is None:
        import redis

        _redis = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis


@celery.task(name="tasks.scan_due_schedules")
def scan_due_schedules() -> int:
    """Beat entrypoint: dispatch one fire_schedule per due pending schedule.
    Returns how many were dispatched (visible in the result backend)."""
    database.init_db()  # worker processes never run the FastAPI lifespan
    db = database.SessionLocal()
    try:
        due = db.scalars(
            select(ScheduledPost)
            .where(ScheduledPost.status == "pending", ScheduledPost.publish_at <= utcnow())
            .order_by(ScheduledPost.publish_at)
        ).all()
        for row in due:
            # The occurrence timestamp pins the task to THIS firing: after a
            # recurring re-arm, a straggler task for the old occurrence is
            # stale by definition (see fire_schedule layer 2).
            fire_schedule.delay(row.id, as_utc(row.publish_at).isoformat())
        if due:
            logger.info("dispatched %d due schedule(s)", len(due))
        return len(due)
    finally:
        db.close()


@celery.task(name="tasks.fire_schedule")
def fire_schedule(schedule_id: str, due_at_iso: str) -> str:
    database.init_db()
    due_at = datetime.fromisoformat(due_at_iso)

    # Layer 1: atomic per-(schedule, occurrence) Redis lock.
    lock_key = f"scheduler:fired:{schedule_id}:{due_at_iso}"
    try:
        acquired = get_redis().set(lock_key, "1", nx=True, ex=FIRE_LOCK_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — Redis down: fall through to the DB guard
        logger.exception("fire lock unavailable for %s — relying on DB guard", schedule_id)
        acquired = True
    if not acquired:
        logger.info("schedule %s occurrence %s already claimed — skipping", schedule_id, due_at_iso)
        return "duplicate"

    db = database.SessionLocal()
    try:
        row = db.get(ScheduledPost, schedule_id)
        # Layer 2: DB re-check — status flipped or publish_at re-armed means
        # this task is a duplicate/stale straggler.
        if row is None or row.status != "pending" or as_utc(row.publish_at) != due_at:
            logger.info("schedule %s no longer due at %s — skipping", schedule_id, due_at_iso)
            return "stale"

        ref = db.get(ContentRef, row.content_id)
        if ref is None:
            # Content vanished (deleted) — nothing to publish, ever.
            row.status = "cancelled"
            db.commit()
            logger.warning("schedule %s points at missing content %s — cancelled",
                           schedule_id, row.content_id)
            return "content_missing"

        fired_at = utcnow()
        row.fire_count += 1
        row.last_fired_at = fired_at
        if row.recurrence:
            row.publish_at = recurrence.next_occurrence(due_at, row.recurrence, row.timezone)
        else:
            row.status = "fired"

        payload = {
            "schedule_id": row.id,
            "account_id": row.account_id,
            "content_id": row.content_id,
            "publish_at": due_at_iso,             # the occurrence that came due
            "fired_at": fired_at.isoformat(),
            "timezone": row.timezone,
            "recurrence": row.recurrence,
            "fire_count": row.fire_count,
            "title": ref.title,
            "image_url": ref.image_url,
            "created_by_user_id": row.created_by_user_id,
        }
        db.commit()  # fire bookkeeping lands before the event is announced
        events.publish(events.FIRE_KEY, payload)
        logger.info("fired schedule %s (content %s, occurrence %s)",
                    schedule_id, row.content_id, due_at_iso)
        return "fired"
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
