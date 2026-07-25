#!/usr/bin/env bash
#
# restart_resilience_test.sh — proves the Definition-of-Done (§10) claim:
#
#   "All inter-service events survive a forced restart of a consumer
#    without data loss or duplication."
#
# It exercises the free-tier STARTER-GRANT flow, which needs no third-party
# keys to run end-to-end:
#
#     POST /auth/signup ─> Auth calls User /internal/accounts/individual
#                          ─> User publishes  account.created  (account_events)
#                          ─> queued in       credits.account_events (durable)
#                          ─> Credits grants   100 credits (once per account)
#
# Two failure modes are checked:
#
#   PHASE 1 — DATA LOSS.  The Credits consumer is STOPPED before the event is
#   produced. The event must sit safely in its durable queue and be granted in
#   full once Credits comes back (nothing lost while the consumer was dead).
#
#   PHASE 2 — DUPLICATION.  The exact same account.created is re-published
#   twice — once under a FRESH event_id (a producer replay) and once under the
#   SAME event_id (a broker redelivery). Neither may add a second grant. This
#   hits both idempotency layers the consumers use:
#       * processed_events(event_id)  — dedupes broker redelivery
#       * a per-account business key   — dedupes a producer re-emit
#
# The whole thing is asserted from the outside: the Postgres `credits` schema
# (credits_ledger + processed_events) and RabbitMQ queue depth are the source
# of truth, not the service logs.
#
# ---------------------------------------------------------------------------
# PREREQUISITES
#   * The full stack is already up:  docker compose up --build -d
#   * AUTH_EXPOSE_DEV_TOKENS=1 (the compose default) so signup needs no email.
#   * Run from the repo root:  ./scripts/restart_resilience_test.sh
#   * Tools: docker compose + curl (both already needed to run the project).
# ---------------------------------------------------------------------------
set -euo pipefail

# docker compose v1 used a hyphen; v2 is a subcommand. Support both.
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
EMAIL="restart-test-$(date +%s)@example.com"
PASSWORD="Sup3rSecret!"           # meets any basic policy; throwaway account
EXPECTED_GRANT="${CREDITS_STARTER_GRANT:-100}"

bold()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die()   { printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# psql inside the postgres container; -tA = tuples only, unaligned (clean scalar).
pg() { $DC exec -T postgres psql -U "${POSTGRES_USER:-creditflow}" -d "${POSTGRES_DB:-creditflow}" -tA -c "$1"; }

# --- count the starter grants + balance for one account, from the ledger ---
grant_count() { pg "SELECT count(*) FROM credits.credits_ledger WHERE account_id='$1' AND entry_type='starter_grant';" | tr -d '[:space:]'; }
balance()     { pg "SELECT COALESCE(SUM(amount),0) FROM credits.credits_ledger WHERE account_id='$1';" | tr -d '[:space:]'; }
queue_depth() { $DC exec -T rabbitmq rabbitmqctl list_queues name messages 2>/dev/null | awk '$1=="credits.account_events"{print $2}'; }

# --- re-publish account.created from inside the credits container ----------
# (it already has creditflow_common + pika + RABBITMQ_URL in its environment.)
republish() { # $1 = account_id  $2 = owner_user_id  $3 = event_id
  $DC exec -T credits python -c "
from creditflow_common.rabbitmq import Publisher
p = Publisher('account_events')
p.publish('account.created',
          {'account_id': '$1', 'type': 'individual', 'name': 'dup', 'plan_tier': 'free', 'owner_user_id': '$2'},
          event_id='$3')
p.close()
print('re-published account.created (event_id=$3)')
"
}

# ===========================================================================
bold "0. Preflight — is the stack up?"
$DC ps --services --filter status=running | grep -qx credits || die "credits service is not running — start the stack first: $DC up -d"
$DC ps --services --filter status=running | grep -qx rabbitmq || die "rabbitmq is not running"
curl -fsS "$GATEWAY/health" >/dev/null 2>&1 || curl -fsS "$GATEWAY/auth/health" >/dev/null 2>&1 || true
ok "stack is running"

# ===========================================================================
bold "PHASE 1 — event survives a dead consumer (no data loss)"

echo "  → stopping the Credits consumer (docker compose stop credits)"
$DC stop credits >/dev/null
ok "credits stopped"

before_depth="$(queue_depth || echo 0)"; before_depth="${before_depth:-0}"

echo "  → signing up $EMAIL while Credits is DOWN"
RESP="$(curl -fsS -X POST "$GATEWAY/auth/signup" \
          -H 'Content-Type: application/json' \
          -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")"
# Portable JSON scrape (no jq): pull the two UUID fields out of the response.
ACCOUNT_ID="$(printf '%s' "$RESP" | grep -o '"account_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
USER_ID="$(printf '%s' "$RESP" | grep -o '"user_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
[ -n "$ACCOUNT_ID" ] || die "signup did not return an account_id (is the User service up?). Response: $RESP"
ok "signed up — account_id=$ACCOUNT_ID"

# The event is now produced but unconsumed: it must be waiting in the queue.
after_depth="$(queue_depth || echo 0)"; after_depth="${after_depth:-0}"
echo "  → credits.account_events queue depth: $before_depth → $after_depth"
[ "$after_depth" -ge 1 ] || die "account.created is not waiting in the durable queue — it was lost"
ok "account.created is safely parked in its durable queue (survived the outage)"

# Sanity: with the consumer down, no grant can exist yet.
[ "$(grant_count "$ACCOUNT_ID")" = "0" ] || die "a grant appeared while Credits was stopped — impossible"
ok "no grant yet (consumer is still down)"

echo "  → restarting Credits (docker compose start credits)"
$DC start credits >/dev/null
echo "  → waiting for the queue to drain…"
for _ in $(seq 1 30); do
  [ "$(grant_count "$ACCOUNT_ID")" = "1" ] && break
  sleep 2
done

[ "$(grant_count "$ACCOUNT_ID")" = "1" ] || die "expected exactly 1 starter grant after restart, got $(grant_count "$ACCOUNT_ID")"
[ "$(balance "$ACCOUNT_ID")" = "$EXPECTED_GRANT" ] || die "balance is $(balance "$ACCOUNT_ID"), expected $EXPECTED_GRANT"
ok "after restart: exactly 1 starter grant, balance = $(balance "$ACCOUNT_ID") — NOTHING LOST"

# ===========================================================================
bold "PHASE 2 — redelivery / replay is deduped (no duplication)"

DUP_EVENT_ID="dup-$(date +%s)-$RANDOM"

echo "  → replay #1: same account, FRESH event_id (a producer re-emit)"
republish "$ACCOUNT_ID" "$USER_ID" "$DUP_EVENT_ID" >/dev/null
echo "  → replay #2: same account, SAME event_id (a broker redelivery)"
republish "$ACCOUNT_ID" "$USER_ID" "$DUP_EVENT_ID" >/dev/null

echo "  → letting Credits consume both replays…"
sleep 6

[ "$(grant_count "$ACCOUNT_ID")" = "1" ] || die "duplication! grant count is $(grant_count "$ACCOUNT_ID") after replays, expected 1"
[ "$(balance "$ACCOUNT_ID")" = "$EXPECTED_GRANT" ] || die "balance drifted to $(balance "$ACCOUNT_ID") after replays, expected $EXPECTED_GRANT"
ok "after two replays: STILL exactly 1 grant, balance = $(balance "$ACCOUNT_ID") — NO DUPLICATION"

# processed_events proves the SAME-event_id replay was recognised and skipped.
seen="$(pg "SELECT count(*) FROM credits.processed_events WHERE event_id='$DUP_EVENT_ID';" | tr -d '[:space:]')"
[ "$seen" = "1" ] || die "expected the replayed event_id to be recorded once in processed_events, got $seen"
ok "processed_events recorded the redelivered event_id exactly once"

bold "RESULT: PASS ✅  — events survived a forced consumer restart with no data loss or duplication."
echo "     (test account $EMAIL / $ACCOUNT_ID was left in place for inspection.)"
