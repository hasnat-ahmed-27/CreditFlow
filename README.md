# CreditFlow — AI Platform (Internship Capstone)

Multi-tenant, credit-based SaaS for AI-assisted content generation and social
publishing. **13 backend microservices** behind a single API Gateway, plus a
React frontend, communicating over **RabbitMQ**, using **Redis** (cache / JWT
session / rate-limit / SSE fan-out), **PostgreSQL** (schema-per-service),
**MongoDB** (scraper), and **Celery** (scheduling).

> Monorepo layout, one repo → one deploy target (single EC2 for the AWS bonus).
> Each service is an independently deployable FastAPI app with its own CI.

## Repository layout

```
creditflow/
├── docker-compose.yml          # infra + all services, one command
├── .env.example                # copy to .env, fill secrets
├── keys/                       # RS256 JWT keypair (private key gitignored)
├── libs/creditflow_common/     # shared: JWT verify, RabbitMQ, idempotency, db
├── services/
│   ├── _template/              # copy this to start a new service
│   ├── gateway/                # 1  API Gateway (JWT verify, routing, rate-limit, SSE)
│   ├── auth/                   # 2  Auth (signup/login/JWT/refresh/reset)
│   ├── user/                   # 3  User / Tenant (accounts, members, invites)
│   ├── billing/                # 4  Billing (Stripe, transactional outbox)
│   ├── credits/                # 5  Credits / Marketplace (ledger)
│   ├── usage/                  # 6  Usage / Metering (Redis + Postgres)
│   ├── ai/                     # 7  AI Generation (OpenRouter, SSE streaming)
│   ├── content/                # 8  Content (drafts, versions, images)
│   ├── scheduler/              # 9  Scheduler (Celery Beat, recurring)
│   ├── social/                 # 10 Social Publishing (LinkedIn OAuth + UGC + Images)
│   ├── scraper/                # 11 Scraper (Playwright/Crawl4AI → MongoDB)
│   ├── notification/           # 12 Notification (Resend/Mailgun email)
│   └── admin/                  # 13 Admin / Ops (sessions, audit log)
├── frontend/                   # React (Vite) + Tailwind
└── .github/workflows/          # per-service CI (path-filtered) + build-push
```

## Tech decisions (locked)

- **Framework:** FastAPI (all services) · **Inter-service:** REST (no gRPC)
- **Auth:** JWT **RS256** — Auth signs with the private key, every service verifies with the public key.
- **Messaging:** RabbitMQ topic exchanges per domain (`billing_events`, `social_events`, `scraper_events`, `usage_events`), durable queues, publisher confirms, DLQ + bounded retry.
- **Reliability:** transactional outbox (Billing), idempotent consumers via a `processed_events` table.
- **Schema:** `Base.metadata.create_all` on startup, no Alembic (spec-sanctioned). Because that only ever creates *missing tables* — it never `ALTER`s one that already exists — each service also declares any column added since its tables first shipped in an `ADDED_COLUMNS` map, topped up idempotently at startup (`creditflow_common.db.add_missing_columns`). Deliberately additive only: no drop, retype, or rename. Needing more than that is the signal to adopt Alembic rather than extend the helper.
- **CI:** each service has a path-filtered workflow (pytest for backend, build for frontend). Images are built and pushed to **GHCR**; the deploy compose pulls them.

## Identity, tenancy, and roles

Every JWT carries `user_id` (`sub`), `account_id`, `role`, and `jti` (spec §6).
All domain data is scoped by `account_id`, never `user_id`.

**Where `account_id`/`role` come from.** The User service owns `accounts` and
`account_members`, so Auth asks it **synchronously** (`services/auth/user_client.py`
→ the User service's `/internal` API) rather than keeping an event-fed read
model. A projection would be eventually consistent at exactly the moment that
matters most — the instant a token is minted — so a member demoted a second
ago could still be handed an `admin` token. This mirrors the AI service's
synchronous quota gate against Usage: decisions that must be *current* are
asked for, not remembered. `/internal/*` is deliberately absent from the
Gateway route table, so it is reachable only from inside the compose network.

The event path still exists: the User service's `user.registered` consumer
provisions the same individual account asynchronously. Both paths share
`services/user/provisioning.py` and are guarded by the same user_id business
key, so whichever arrives first wins and the other is a no-op.

| Flow | Behaviour | If the User service is down |
|---|---|---|
| Signup | Creates the individual Account (type `individual`, user as Owner) | Best effort — the `user.registered` consumer provisions it |
| Login | Mints a token scoped to that real account | **503** — never mint a guessed `account_id` (it would scope other services to the wrong tenant) |
| `POST /auth/switch-account` | New account-scoped JWT for any account the caller belongs to (§4 Account Switcher); revokes the previous access session | **503** — membership is never assumed |
| `POST /auth/refresh` | Re-resolves the role, so a demotion bites on the next rotation; a removal ends the session | Falls back to the role stored on the refresh row — a blip must not log everyone out |

**Platform SuperAdmin** (§8 Service 13) is a `users.is_superadmin` flag, not an
`account_members` role — a SuperAdmin's authority does not come from belonging
to any account, so no account owner can grant it. It is designated by the
`SUPERADMIN_EMAILS` env var and reconciled on Auth startup in **both**
directions: listed addresses are granted the role, and an address removed from
the list is demoted with its live sessions revoked. There is no promote
endpoint — a self-service escalation surface, guarded by a role you'd need to
already hold, isn't worth the bootstrap problem it creates. A SuperAdmin's
token carries `role: "superadmin"` (the exact string the Admin service already
gates on) alongside a real `account_id`, so account-scoped services keep
working for them. They still cannot *switch into* an account they don't belong
to: the platform role grants cross-account visibility through the Admin
service, not membership.

## Credits

The ledger is append-only (`credits_ledger`) — every change is a row, the
balance is `SUM(amount)`, nothing is ever mutated in place.

**AI generation deducts credits** (§10) via the Credits service consuming
`ai.generation_completed` off `usage_events`, not via the AI service calling a
debit endpoint. The spec's contract for Service 7 is *Consumes: none*, and a
synchronous call would put Credits inside the generation request path — a
Credits outage would then fail generations the provider had already billed.
As an event, the debit simply waits in `credits.usage_events` until the service
is back. Usage and Content already consume the same event; Credits joins the
fan-out.

**Price:** `ceil(total_tokens / 1000) * CREDITS_PER_1K_TOKENS`, minimum 1
credit per metered generation (`services/credits/ledger.py`). Priced in tokens
rather than provider dollars because tokens are the unit the platform already
meters in, credits are a product currency that shouldn't move with a vendor's
rate card, and a deterministic formula makes the debit reproducible from the
event payload alone.

**Idempotency:** `processed_events` on `event_id` (broker redelivery) *plus* a
`job_id` business key on the ledger (the producer re-emitting under a fresh
`event_id`). Same two layers the invoice/refund paths use.

**Insufficient balance:** the debit lands anyway and the balance may go
negative. By the time the event exists the tokens are streamed and the provider
has charged us; refusing to record it wouldn't un-spend them, it would hide the
debt and let the account keep generating free. Overspend is *prevented* up
front by the AI service's synchronous quota gate — the right place for a "no".
`credits.low_balance` fires on the threshold crossing **or** the moment the
balance goes into debt, carrying `insufficient`.

## Running locally

```bash
cp .env.example .env          # fill in secrets
docker compose up --build     # infra + services
```

Infra ports (host): Postgres 5432 · Redis 6379 · RabbitMQ 5672 (UI 15672) · MongoDB 27017 · Gateway 8080 · Frontend 5173.

## Build order (phases)

0. **Infra + shared libs + template + CI** ← you are here
1. **Auth foundation** — auth, user, gateway + frontend auth/onboarding
2. **Money** — billing (Stripe + outbox), credits/marketplace, usage
3. **AI core** — ai (OpenRouter SSE), content, notification
4. **Schedule + publish** — scheduler (Celery Beat + recurring), social (LinkedIn + images), scraper
5. **Admin + reliability hardening** — admin/ops, DLQ + idempotency wired everywhere
6. **Bonus** — AWS deploy, AI image generation

## Git workflow

Branches: `main` (prod, protected), `dev` (integration, protected), `feature/*`, `fix/*`.
PRs into `dev`; **CI must pass to merge**. Conventional Commits (`feat:`, `fix:`, `chore:` …).
