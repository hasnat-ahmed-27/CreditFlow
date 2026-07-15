"""
RabbitMQ consumers for the Notification service — the spec's "consumer of
all domain events" that turns them into emails.

QUEUE CONTRACT — we consume the exact durable queues the producing services
PRE-DECLARED for us (their events.py modules bind the keys; messages have
been accumulating there since each producer shipped, so the backlog drains
the moment this service comes up). Re-declaring on connect is idempotent,
same contract as every other service. One consumer thread per queue (the
shared pika loop is blocking), all funnelling into the ONE handle_event
below — tests call it directly, exactly like the other services.

  queue                          exchange          keys we bind
  notifications.user_events      user_events       user.*  (Auth declared it)
  notifications.account_events   account_events    account.* member.* invite.*
  notifications.billing_events   billing_events    invoice.* payment.*
                                                   subscription.* refund.*
  notifications.usage_events     usage_events      usage.threshold_reached,
                                                   ai.generation_failed (the
                                                   AI service bound its
                                                   failures into our queue)
  notifications.credits_events   credits_events    credits.low_balance
  notifications.social_events    social_events     post.published, post.failed

The bindings mirror what each producer already declared — bindings are
additive in RabbitMQ, so re-binding the same keys changes nothing on the
broker.

Not every key in those queues produces an email: templates.render() returns
None for the rest (user.logged_in, account.*, subscription.updated,
refund.issued, ai.generation_failed, ...) and we record the event as
processed and move on — the spec's notification list, not the queue
contents, decides what gets sent.

Idempotency (spec §7 — a redelivered event never double-sends an email):
`already_processed(db, event_id)` + the processed_events table dedupe broker
redeliveries; the event row, any read-model upserts, and the
notification_log row commit in the SAME transaction, so either the event is
recorded as done WITH its outcome or neither exists. TRANSIENT provider
failures (network, 429/5xx from Resend AND Mailgun) raise before any commit
— the shared consumer retries with backoff and dead-letters after
MAX_RETRIES — while PERMANENT failures (bad recipient, no provider
configured, no known address for the account) conclude as a
status=failed log row. notification.sent is emitted only AFTER the commit
(commit-first rule), and only for status=sent.

The consumer threads are daemons (see main.py); each loop reconnects forever
so a broker restart doesn't kill the service.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from creditflow_common import rabbitmq
from creditflow_common.idempotency import already_processed

import database
import events
import mailer
import recipients
import templates
from models import NotificationLog

logger = logging.getLogger("notification.consumer")


@dataclass(frozen=True)
class Binding:
    exchange: str
    queue: str
    routing_keys: tuple[str, ...]


BINDINGS: tuple[Binding, ...] = (
    Binding("user_events", "notifications.user_events",
            ("user.*",)),
    Binding("account_events", "notifications.account_events",
            ("account.*", "member.*", "invite.*")),
    Binding("billing_events", "notifications.billing_events",
            ("invoice.*", "payment.*", "subscription.*", "refund.*")),
    Binding("usage_events", "notifications.usage_events",
            ("usage.threshold_reached", "ai.generation_failed")),
    Binding("credits_events", "notifications.credits_events",
            ("credits.low_balance",)),
    Binding("social_events", "notifications.social_events",
            ("post.published", "post.failed")),
)


def handle_event(routing_key: str, data: dict, event_id: str) -> None:
    db = database.SessionLocal()
    try:
        if already_processed(db, event_id):
            db.commit()
            logger.info("skipping already-processed event %s (%s)", event_id, routing_key)
            return

        # Learn addresses first: the SAME event can both feed the read model
        # and need an email (member.joined), and the rows commit together.
        recipients.apply_event(db, routing_key, data)

        rendered = templates.render(routing_key, data)
        if rendered is None:
            db.commit()  # non-notifiable key: record the event, send nothing
            return

        # Tenant scope for the log/read routes. user_events carry no
        # account_id; user_id is the same value under the placeholder-JWT
        # convention (see models.py).
        account_id = data.get("account_id") or data.get("user_id")

        to = _resolve_recipient(db, rendered, data)
        if not to:
            db.add(NotificationLog(
                event_id=event_id, routing_key=routing_key,
                template=rendered.template, account_id=account_id,
                recipient=None, subject=rendered.subject, status="failed",
                error=f"no known recipient for account {account_id}",
            ))
            db.commit()
            logger.warning("%s %s: no known recipient for account %s",
                           routing_key, event_id, account_id)
            return

        try:
            provider, message_id = mailer.send(to, rendered.subject, rendered.body)
        except mailer.MailerTransientError:
            raise  # broker retry/backoff -> DLQ — nothing committed
        except mailer.MailerError as exc:
            db.add(NotificationLog(
                event_id=event_id, routing_key=routing_key,
                template=rendered.template, account_id=account_id,
                recipient=to, subject=rendered.subject, status="failed",
                error=str(exc)[:2000],
            ))
            db.commit()
            logger.warning("%s %s: send failed permanently: %s", routing_key, event_id, exc)
            return

        row = NotificationLog(
            event_id=event_id, routing_key=routing_key,
            template=rendered.template, account_id=account_id,
            recipient=to, subject=rendered.subject, provider=provider,
            provider_message_id=message_id or None, status="sent",
        )
        db.add(row)
        db.commit()  # processed_events + read model + log row land atomically
        events.publish(events.SENT_KEY, {
            "notification_id": row.id,
            "event_id": event_id,
            "routing_key": routing_key,
            "account_id": account_id,
            "recipient": to,
            "template": rendered.template,
            "provider": provider,
            "provider_message_id": message_id or None,
        })
        logger.info("%s -> %s via %s (%s)", routing_key, to, provider, rendered.template)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _resolve_recipient(db, rendered: templates.Rendered, data: dict) -> str | None:
    if rendered.recipient_source == templates.FROM_PAYLOAD:
        return (data.get("email") or "").strip() or None
    return recipients.resolve_account_email(db, data.get("account_id"))


def _run_binding(binding: Binding) -> None:
    """Blocking consume loop with reconnect for ONE queue."""
    while True:
        try:
            rabbitmq.consume(
                exchange=binding.exchange,
                queue=binding.queue,
                routing_keys=list(binding.routing_keys),
                handler=handle_event,
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
                             name=f"notification-consumer-{binding.queue}", daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
