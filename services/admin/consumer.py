"""
RabbitMQ consumer for the Admin service — the spec's "consume all domain
events into an audit_log table" ("Consumes: all events, topic-bound (#) for
audit purposes").

QUEUE CONTRACT — one durable queue per domain exchange, named
`admin.<exchange>` (the `<consumer>.<exchange>` convention every
pre-declared queue in this repo follows), each bound with the `#` wildcard
so every current AND future routing key on that exchange lands in the audit
log. `admin.notification_events` was PRE-DECLARED by the Notification
service (bound to notification.sent — the only key on that exchange), so its
backlog drains the moment this service comes up; re-declaring with `#` is
additive and idempotent, same contract as every other service. One consumer
thread per queue (the shared pika loop is blocking), all funnelling into the
ONE handle_event below — tests call it directly, exactly like the other
services.

  queue                       exchange             producer
  admin.user_events           user_events          Auth
  admin.account_events        account_events       User/Tenant
  admin.billing_events        billing_events       Billing
  admin.credits_events        credits_events       Credits
  admin.usage_events          usage_events         Usage + AI (both publish here)
  admin.content_events        content_events       Content
  admin.scheduler_events      scheduler_events     Scheduler
  admin.social_events         social_events        Social Publishing
  admin.scraper_events        scraper_events       Scraper
  admin.notification_events   notification_events  Notification (pre-declared for us)

Idempotency (spec §7 — a redelivered event never appends twice):
`already_processed(db, event_id)` + the processed_events table dedupe broker
redeliveries; the dedup row, the audit_log row, and any directory read-model
upserts commit in the SAME transaction, so either the event is fully
recorded or not at all. This service publishes NOTHING (spec: "Publishes:
none"), so there is no commit-then-emit ordering to worry about.

The consumer threads are daemons (see main.py); each loop reconnects forever
so a broker restart doesn't kill the service.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from creditflow_common import rabbitmq
from creditflow_common.idempotency import already_processed

import database
from models import AccountDirectory, AuditLog, UserDirectory

logger = logging.getLogger("admin.consumer")


@dataclass(frozen=True)
class Binding:
    exchange: str
    queue: str
    routing_keys: tuple[str, ...]


# Every domain exchange in the platform, `#`-bound per spec.
EXCHANGES: tuple[str, ...] = (
    "user_events",
    "account_events",
    "billing_events",
    "credits_events",
    "usage_events",
    "content_events",
    "scheduler_events",
    "social_events",
    "scraper_events",
    "notification_events",
)

BINDINGS: tuple[Binding, ...] = tuple(
    Binding(exchange, f"admin.{exchange}", ("#",)) for exchange in EXCHANGES
)

# Payload fields that name the acting user, most-specific first. `user_id`
# last: on several events it is the SUBJECT, but for Auth's user.* events it
# is also the actor, and no better field exists.
_ACTOR_FIELDS = (
    "created_by_user_id",
    "updated_by_user_id",
    "invited_by_user_id",
    "requested_by_user_id",
    "owner_user_id",
    "user_id",
)


def _extract_actor(data: dict) -> str | None:
    for field in _ACTOR_FIELDS:
        if data.get(field):
            return str(data[field])
    return None


def handle_event(routing_key: str, data: dict, event_id: str,
                 exchange: str | None = None) -> None:
    db = database.SessionLocal()
    try:
        if already_processed(db, event_id):
            db.commit()
            logger.info("skipping already-processed event %s (%s)", event_id, routing_key)
            return

        data = data if isinstance(data, dict) else {}
        # user_events carry no account_id; user_id is the same value under
        # the placeholder-JWT convention (see models.py).
        account_id = data.get("account_id") or data.get("user_id")

        db.add(AuditLog(
            event_id=event_id,
            exchange=exchange,
            routing_key=routing_key,
            account_id=str(account_id) if account_id else None,
            actor_user_id=_extract_actor(data),
            payload=json.dumps(data, default=str),
        ))
        _apply_directory(db, routing_key, data)
        db.commit()  # processed_events + audit row + directory upserts land atomically
        logger.info("audited %s (%s) account=%s", routing_key, event_id, account_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _apply_directory(db, routing_key: str, data: dict) -> None:
    """Learn directory rows from the events that carry identity data. Only
    identity fields are touched — never this service's suspended flag."""
    if routing_key == "user.registered" and data.get("user_id"):
        _upsert_user(db, str(data["user_id"]), data.get("email"))

    elif routing_key == "member.joined":
        if data.get("user_id"):
            _upsert_user(db, str(data["user_id"]), data.get("email"))
        if data.get("account_id"):
            _upsert_account(db, str(data["account_id"]), name=data.get("account_name"))

    elif routing_key == "account.created" and data.get("account_id"):
        _upsert_account(
            db, str(data["account_id"]),
            name=data.get("name"), type_=data.get("type"),
            plan_tier=data.get("plan_tier"), owner_user_id=data.get("owner_user_id"),
        )

    elif routing_key == "account.updated" and data.get("account_id"):
        if data.get("change") == "profile":
            _upsert_account(db, str(data["account_id"]), name=data.get("name"))

    elif routing_key == "invoice.paid" and data.get("account_id"):
        # Billing's invoice.paid carries the purchased plan — keeps the
        # directory's plan_tier current without calling the User service.
        _upsert_account(db, str(data["account_id"]), plan_tier=data.get("plan"))


def _upsert_account(db, account_id: str, name: str | None = None,
                    type_: str | None = None, plan_tier: str | None = None,
                    owner_user_id: str | None = None) -> None:
    row = db.get(AccountDirectory, account_id)
    if row is None:
        row = AccountDirectory(account_id=account_id)
        db.add(row)
    if name:
        row.name = name
    if type_:
        row.type = type_
    if plan_tier:
        row.plan_tier = plan_tier
    if owner_user_id:
        row.owner_user_id = str(owner_user_id)


def _upsert_user(db, user_id: str, email: str | None) -> None:
    row = db.get(UserDirectory, user_id)
    if row is None:
        row = UserDirectory(user_id=user_id)
        db.add(row)
    if email:
        row.email = email


def _run_binding(binding: Binding) -> None:
    """Blocking consume loop with reconnect for ONE queue."""
    while True:
        try:
            rabbitmq.consume(
                exchange=binding.exchange,
                queue=binding.queue,
                routing_keys=list(binding.routing_keys),
                handler=lambda rk, data, eid, b=binding: handle_event(
                    rk, data, eid, exchange=b.exchange),
            )
        except Exception:  # noqa: BLE001 — broker hiccup: log, back off, reconnect
            logger.exception("consumer for %s lost connection — retrying in 5s", binding.queue)
            time.sleep(5)


def run() -> None:
    """Start one daemon consumer thread per queue and wait on them — target
    for the single thread main.py spawns."""
    threads = []
    for binding in BINDINGS:
        t = threading.Thread(target=_run_binding, args=(binding,),
                             name=f"admin-consumer-{binding.queue}", daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
