"""
rabbitmq.py — reliable messaging helpers (pika, blocking).

Implements the reliability rules required by the spec:
  - Durable topic exchanges per domain.
  - Durable/persistent messages (delivery_mode=2).
  - Publisher confirms enabled on every publish.
  - Every queue bound to a Dead-Letter Exchange (DLX) with a bounded retry count.
  - A stable `event_id` header on every message so consumers can be idempotent
    (see idempotency.py).

Usage (publisher):
    pub = Publisher(exchange="billing_events")
    pub.publish("invoice.paid", {"account_id": "...", "amount": 500})

Usage (consumer):
    consume(
        exchange="billing_events",
        queue="credits.invoice_paid",
        routing_keys=["invoice.paid", "refund.issued"],
        handler=my_handler,          # (routing_key, payload, event_id) -> None
    )
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable, Iterable

import pika

from . import config

MAX_RETRIES = 5


def _connect() -> pika.BlockingConnection:
    params = pika.URLParameters(config.RABBITMQ_URL)
    params.heartbeat = 30
    params.blocked_connection_timeout = 30
    return pika.BlockingConnection(params)


class Publisher:
    """One durable topic exchange, publisher confirms on, persistent messages."""

    def __init__(self, exchange: str):
        self.exchange = exchange
        self._conn = _connect()
        self._ch = self._conn.channel()
        self._ch.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
        self._ch.confirm_delivery()  # publisher confirms

    def publish(self, routing_key: str, payload: dict, event_id: str | None = None) -> str:
        event_id = event_id or str(uuid.uuid4())
        body = json.dumps({"event_id": event_id, "routing_key": routing_key, "data": payload})
        self._ch.basic_publish(
            exchange=self.exchange,
            routing_key=routing_key,
            body=body.encode("utf-8"),
            properties=pika.BasicProperties(
                delivery_mode=2,             # persistent
                content_type="application/json",
                message_id=event_id,
                headers={"event_id": event_id, "x-retries": 0},
            ),
            mandatory=True,                  # raises if unroutable (with confirms on)
        )
        return event_id

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


def declare_with_dlx(ch, exchange: str, queue: str, routing_keys: Iterable[str]) -> None:
    """Declare a durable queue bound to `exchange`, with a dead-letter exchange."""
    dlx = f"{exchange}.dlx"
    dlq = f"{queue}.dlq"
    ch.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
    ch.exchange_declare(exchange=dlx, exchange_type="fanout", durable=True)

    # Main queue routes rejected messages to the DLX.
    ch.queue_declare(queue=queue, durable=True, arguments={"x-dead-letter-exchange": dlx})
    for rk in routing_keys:
        ch.queue_bind(queue=queue, exchange=exchange, routing_key=rk)

    # Dead-letter queue collects poison messages after retries are exhausted.
    ch.queue_declare(queue=dlq, durable=True)
    ch.queue_bind(queue=dlq, exchange=dlx)


def consume(
    exchange: str,
    queue: str,
    routing_keys: Iterable[str],
    handler: Callable[[str, dict, str], None],
) -> None:
    """
    Blocking consumer loop. Calls handler(routing_key, data, event_id).
    On handler success -> ack. On failure -> retry up to MAX_RETRIES, then reject
    to the DLQ (nack without requeue so the DLX takes it).
    """
    conn = _connect()
    ch = conn.channel()
    declare_with_dlx(ch, exchange, queue, routing_keys)
    ch.basic_qos(prefetch_count=10)

    def _on_message(chan, method, props, body):
        headers = (props.headers or {})
        event_id = headers.get("event_id") or (props.message_id or str(uuid.uuid4()))
        retries = int(headers.get("x-retries", 0))
        try:
            msg = json.loads(body)
            handler(method.routing_key, msg.get("data", {}), event_id)
            chan.basic_ack(delivery_tag=method.delivery_tag)
        except Exception:  # noqa: BLE001
            if retries + 1 >= MAX_RETRIES:
                # Exhausted -> dead-letter (do not requeue).
                chan.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            else:
                # Re-publish with an incremented retry count, then ack the original.
                time.sleep(min(2 ** retries, 30))
                new_headers = {**headers, "x-retries": retries + 1, "event_id": event_id}
                chan.basic_publish(
                    exchange=exchange,
                    routing_key=method.routing_key,
                    body=body,
                    properties=pika.BasicProperties(
                        delivery_mode=2, content_type="application/json",
                        message_id=event_id, headers=new_headers,
                    ),
                )
                chan.basic_ack(delivery_tag=method.delivery_tag)

    ch.basic_consume(queue=queue, on_message_callback=_on_message)
    ch.start_consuming()
