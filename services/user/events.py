"""
Domain-event publishing for the user/tenant service.

Publishes to the durable `account_events` topic exchange (publisher confirms +
persistent messages come from creditflow_common.rabbitmq.Publisher).

Routing keys (per spec): account.created, account.updated, member.joined —
plus invite.created, which carries the invite token the Notification service
turns into the invitee's email.

Same best-effort posture as the auth service: a broker outage is logged but
does not fail the user-facing request (the transactional outbox is a
Billing-service requirement, not ours).
"""
from __future__ import annotations

import logging

from creditflow_common.rabbitmq import Publisher, declare_with_dlx

logger = logging.getLogger("user.events")

EXCHANGE = "account_events"

# Pre-declared durable queues for our consumers (same contract as
# auth/billing). The shared Publisher sends with mandatory=True, so without at
# least one bound queue our events would bounce as unroutable — and
# invite.created carries the invite token, which must not be lost. Declaring
# them HERE, at publish time, is also what makes the events survive a consumer
# that does not exist yet: the queue is already collecting.
#   - notifications.account_events — Notification service, bound to every key
#     we publish (invites, membership, account lifecycle).
#   - credits.account_events — Credits service opens each new account's
#     free-tier balance off account.created (spec §4, the Free plan's starter
#     credit grant). Pre-declared because signup and the Credits service come
#     up independently: an account created while Credits is down must still
#     be granted its credits when Credits returns, or the user's first AI
#     generation opens on a negative balance.
PREDECLARED_QUEUES: dict[str, list[str]] = {
    "notifications.account_events": ["account.*", "member.*", "invite.*"],
    "credits.account_events": ["account.created"],
}

_publisher: Publisher | None = None


def publish(routing_key: str, payload: dict) -> str | None:
    """Returns the event_id, or None if the broker was unreachable."""
    global _publisher
    try:
        if _publisher is None:
            _publisher = Publisher(exchange=EXCHANGE)
            for queue, keys in PREDECLARED_QUEUES.items():
                declare_with_dlx(_publisher._ch, EXCHANGE, queue, keys)
        return _publisher.publish(routing_key, payload)
    except Exception:  # noqa: BLE001 — broker down must not 500 the request
        logger.exception("failed to publish %s to %s", routing_key, EXCHANGE)
        _publisher = None  # force a fresh connection on the next attempt
        return None
