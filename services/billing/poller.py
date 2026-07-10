"""
Background poller — the read side of the transactional outbox, plus the
dunning ticker. Started as a daemon thread from main.py's lifespan (set
BILLING_POLLER_ENABLED=0 to run it elsewhere; `python -m poller` runs the
same loop as a standalone process if you'd rather ship it as its own
container/command).

Each tick:
  1. dunning.apply_due()   — expire grace periods (stages downgrade events)
  2. outbox.publish_pending() — push unpublished outbox rows to RabbitMQ

Dunning runs first so a downgrade's event goes out on the same tick. A tick
that finds the broker down publishes nothing and simply tries again next
interval — the outbox rows are the durable queue in the meantime.
"""
from __future__ import annotations

import logging
import time

from creditflow_common import config

import database
import dunning
import outbox

logger = logging.getLogger("billing.poller")

POLL_INTERVAL_SECONDS = float(config.env("BILLING_POLL_INTERVAL_SECONDS", "2"))


def tick() -> tuple[int, int]:
    """One poll cycle. Returns (events_published, accounts_downgraded)."""
    db = database.SessionLocal()
    try:
        downgraded = dunning.apply_due(db)
        published = outbox.publish_pending(db)
        return published, len(downgraded)
    finally:
        db.close()


def run() -> None:
    """Blocking loop — daemon thread target (or standalone via `-m poller`)."""
    logger.info("outbox poller started (interval %.1fs)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 — a bad tick must not kill the loop
            logger.exception("poller tick failed")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    database.init_db()
    run()
