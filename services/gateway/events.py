"""
RabbitMQ publisher wiring for the API Gateway — spec §8 Service 1 Event
Contract: "Publishes: billing.*, social.*, ai.* (relayed from webhooks).
Consumes: none — publisher only." Durable topic exchanges, publisher
confirms, persistent messages, all via creditflow_common.rabbitmq.Publisher
like every other service.

EXCHANGE CHOICE follows the platform's exchange-per-domain rule, so a
gateway-relayed event lands on the same exchange as the domain it describes
and the Admin audit log picks it up with no new binding:

    billing.*  -> billing_events     (Billing's own exchange)
    social.*   -> social_events      (Social Publishing's exchange)
    ai.*       -> usage_events       (where ai.* lives — see ai/events.py)

ROUTING KEYS ARE ONE SEGMENT PER DOMAIN. A Stripe `invoice.paid` becomes
`billing.invoice_paid`, not `billing.invoice.paid`: an AMQP `*` matches
exactly one word, so the spec's `billing.*` contract only holds if the
provider's dotted type is flattened. The flattening also guarantees the
gateway can never collide with a service's own keys — Billing publishes
`invoice.paid`, we publish `billing.invoice_paid`, and a consumer bound to
one never sees the other. That is what keeps "relay" from turning into
"double-process".

Pre-declared durable queues (same contract as every other publisher here):
the shared Publisher sends with mandatory=True, so every routing key needs at
least one bound queue or the publish bounces as unroutable. The Admin service
binds `#` on all ten exchanges for its audit log (admin/consumer.py), so we
declare exactly those queues — identical name, identical binding, idempotent
if Admin got there first, and gateway events accumulate durably from day one
even if Admin is down.

Posture: best-effort. A broker outage is logged and returns None; it never
fails the webhook. Losing the announcement is survivable because the gateway
is not the system of record for any of these events — the provider retries,
and for Stripe the Billing service's persist-before-process is the durable
truth (see webhooks.py).
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("gateway.events")

# Routing-key domain -> the domain's topic exchange.
EXCHANGE_BY_DOMAIN: dict[str, str] = {
    "billing": "billing_events",
    "social": "social_events",
    "ai": "usage_events",
}

# The Admin service's audit queue on each exchange we publish to.
PREDECLARED_QUEUES: dict[str, list[str]] = {
    exchange: ["#"] for exchange in set(EXCHANGE_BY_DOMAIN.values())
}

_publishers: dict[str, Publisher] = {}


def _publisher_for(exchange: str) -> Publisher:
    publisher = _publishers.get(exchange)
    if publisher is None:
        publisher = Publisher(exchange=exchange)
        for queue, keys in PREDECLARED_QUEUES.items():
            if queue == exchange:
                declare_with_dlx(publisher._ch, exchange, f"admin.{exchange}", keys)
        _publishers[exchange] = publisher
    return publisher


def publish(routing_key: str, payload: dict, event_id: str | None = None) -> str | None:
    """Publish one normalized webhook event. Returns the event_id, or None if
    the broker was unreachable.

    BLOCKING (pika): call it from a worker thread, never straight from the
    async request path — webhooks.py wraps it in run_in_threadpool.
    """
    domain = routing_key.split(".", 1)[0]
    exchange = EXCHANGE_BY_DOMAIN.get(domain)
    if exchange is None:
        raise ValueError(f"No exchange for routing key domain: {domain!r}")
    try:
        return _publisher_for(exchange).publish(routing_key, payload, event_id=event_id)
    except Exception:  # noqa: BLE001 — broker down must not fail the webhook
        logger.exception("failed to publish %s to %s", routing_key, exchange)
        _publishers.pop(exchange, None)  # force a fresh connection next attempt
        return None


def close() -> None:
    """Lifespan teardown — close every open broker connection."""
    for publisher in _publishers.values():
        publisher.close()
    _publishers.clear()
