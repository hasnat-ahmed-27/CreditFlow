# CreditFlow — AI Platform

> ### 🔗 Live demo: **http://13.60.163.115:3000**
> Deployed on a single AWS EC2 instance via `docker compose` — the full 13-service stack, running 24/7.

[![backend-ci](https://github.com/hasnat-ahmed-27/CreditFlow/actions/workflows/backend-ci.yml/badge.svg)](https://github.com/hasnat-ahmed-27/CreditFlow/actions/workflows/backend-ci.yml)
[![frontend-ci](https://github.com/hasnat-ahmed-27/CreditFlow/actions/workflows/frontend-ci.yml/badge.svg)](https://github.com/hasnat-ahmed-27/CreditFlow/actions/workflows/frontend-ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-Vite-61DAFB?logo=react&logoColor=black)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![RabbitMQ](https://img.shields.io/badge/RabbitMQ-events-FF6600?logo=rabbitmq&logoColor=white)

Multi-tenant, credit-based SaaS for AI-assisted content generation and social
publishing. **13 independently deployable backend microservices** behind a
single API Gateway, plus a React frontend, communicating asynchronously over
**RabbitMQ** and using **Redis** (cache / JWT session state / rate-limiting /
SSE fan-out), **PostgreSQL** (one instance, schema-per-service), **MongoDB**
(scraper only), and **Celery** (scheduling).

Accounts (individual or team) buy credits, spend them on AI generations, and
can list/buy/transfer credits with other accounts through an internal
marketplace.

> **Status.** The full system is **deployed live** on a single AWS EC2 instance
> ([http://13.60.163.115:3000](http://13.60.163.115:3000)) via `docker compose`,
> and runs identically on a local machine. **Every integration is proven
> end-to-end, live:**
> - **OpenRouter** — AI generation streaming token-by-token via SSE, with credit deduction
> - **Stripe** (test mode) — hosted checkout → webhook → subscription active → credit grant → balance updates
> - **LinkedIn** — OAuth connect + **image publishing to a real feed** (register-upload → asset URN → UGC post)
> - **Resend** — transactional email (signup verification, password-reset OTP)
>
> Both mandatory bonuses (recurring schedules, LinkedIn image publishing) and the
> AWS single-machine deployment bonus are done. A real event dead-lettering bug
> was found and fixed during testing. Solo build; every change went through a
> CI-gated PR into `dev`. See [dev-only affordances](#dev-only-affordances--known-gaps)
> for the honest small print.

---

## Table of contents

1. [System architecture](#system-architecture)
2. [Data ownership & event flows](#data-ownership--event-flows)
3. [Local setup](#local-setup)
4. [Environment variables — every key & how to get it](#environment-variables--every-key--how-to-get-it)
5. [Ports](#ports)
6. [First-run walkthrough](#first-run-walkthrough)
7. [Event-reliability design](#event-reliability-design)
8. [Restart-resilience test (§10)](#restart-resilience-test-10)
9. [Running the tests](#running-the-tests)
10. [Git & CI workflow](#git--ci-workflow)
11. [Dev-only affordances & known gaps](#dev-only-affordances--known-gaps)
12. [AWS free-tier constraints & tradeoffs](#aws-free-tier-constraints--tradeoffs)

---

## System architecture

One monorepo, one deploy target. Each service is a self-contained FastAPI app
with its own Postgres schema (or Mongo, for the scraper), its own tests, and its
own path-filtered CI. The Gateway is the only public entry point; everything
else is reachable only inside the compose network.

```
creditflow/
├── docker-compose.yml          # infra + all services + workers, one command
├── .env.example                # copy to .env, fill secrets
├── ruff.toml                   # lint config (services/ + libs/), shared by CI
├── keys/                       # RS256 JWT keypair (private key gitignored)
├── libs/creditflow_common/     # shared: JWT, RabbitMQ helpers, idempotency, db
├── scripts/
│   └── restart_resilience_test.sh   # §10 exactly-once proof (see below)
├── services/
│   ├── _template/              # copy this to start a new service
│   ├── gateway/                # 1  API Gateway (JWT verify, routing, rate-limit, SSE, webhooks)
│   ├── auth/                   # 2  Auth (signup/login/JWT/refresh/reset, SuperAdmin reconcile)
│   ├── user/                   # 3  User / Tenant (accounts, members, invites)
│   ├── billing/                # 4  Billing (Stripe + transactional outbox)
│   ├── credits/                # 5  Credits / Marketplace (append-only ledger)
│   ├── usage/                  # 6  Usage / Metering (Redis counters + Postgres)
│   ├── ai/                     # 7  AI Generation (OpenRouter SSE streaming)
│   ├── content/                # 8  Content (drafts, versions, images)
│   ├── scheduler/              # 9  Scheduler (Celery Beat + recurring)
│   ├── social/                 # 10 Social Publishing (LinkedIn OAuth + UGC + images)
│   ├── scraper/                # 11 Scraper (Playwright/Crawl4AI → MongoDB)
│   ├── notification/           # 12 Notification (Resend/Mailgun email)
│   └── admin/                  # 13 Admin / Ops (sessions, per-account rollups, audit log)
├── frontend/                   # React (Vite) + Tailwind, served by nginx in the container
└── .github/workflows/          # backend-ci (lint+tests) · frontend-ci (lint+build) · build-push
```

**Compose runs 17 app processes, not 13.** The Scheduler and Scraper each build
one image but run **three** processes: the FastAPI API, a Celery **worker**, and
a Celery **beat** scheduler (`scheduler-worker` / `scheduler-beat` /
`scraper-worker` / `scraper-beat`). Only the APIs publish host ports; the
workers and beats are internal.

### Locked tech decisions

- **Framework:** FastAPI everywhere · **Inter-service sync calls:** REST (no gRPC).
- **Auth:** JWT **RS256** — only Auth holds the private key and signs; every
  other service (and the Gateway) loads the public key and verifies. A verifier
  can never mint.
- **Messaging:** RabbitMQ **topic exchanges per domain** (`billing_events`,
  `social_events`, `scraper_events`, `usage_events`, plus `account_events`),
  durable queues, persistent messages, publisher confirms, and a dead-letter
  exchange per queue with bounded retry.
- **Reliability:** transactional **outbox** (Billing) and **idempotent
  consumers** via a per-service `processed_events` table plus a domain business
  key. See [Event-reliability design](#event-reliability-design).
- **Schema:** `Base.metadata.create_all` on startup (no Alembic — spec-sanctioned).
  Because `create_all` only ever creates *missing* tables and never `ALTER`s an
  existing one, each service also declares columns/indexes added after its
  tables first shipped in an `ADDED_COLUMNS` map, topped up idempotently at
  startup (`creditflow_common.db.add_missing_columns`). Deliberately additive
  only — needing a drop/retype/rename is the signal to adopt Alembic.

---

## Data ownership & event flows

Every service owns its own data and is the only writer of it. Others learn about
changes through events, never by reaching into another service's tables.

| # | Service | Host port | Primary store | Publishes | Consumes |
|---|---------|-----------|---------------|-----------|----------|
| 1 | Gateway | **8080** (public) | Redis (rate-limit + webhook dedup) | normalized provider webhooks | — |
| 2 | Auth | 8001 | Postgres `auth` + Redis (jti sessions) | `user.registered` | — |
| 3 | User/Tenant | 8002 | Postgres `user` | `account.created`, `account.updated`, `member.joined` | `user.registered` |
| 4 | Billing | 8003 | Postgres `billing` | `invoice.paid`, `payment.failed`, `subscription.*`, `refund.issued` (via outbox) | Stripe webhooks (relayed by Gateway) |
| 5 | Credits/Marketplace | 8004 | Postgres `credits` | `credits.credited`, `credits.debited`, `credits.low_balance` | `invoice.paid`, `refund.issued`, `ai.generation_completed`, `account.created` |
| 6 | Usage/Metering | 8005 | Redis + Postgres `usage` | `usage.threshold_reached` | `ai.generation_completed` |
| 7 | AI Generation | 8006 | Postgres `ai` (+ Redis pub/sub for SSE) | `ai.generation_completed`, `ai.generation_failed` | — |
| 8 | Content | 8007 | Postgres `content` | `content.created`, `content.updated` | `ai.generation_completed` |
| 9 | Scheduler | 8008 | Postgres `scheduler` + Redis (Celery) | `content.scheduled` | `content.created` |
| 10 | Social Publishing | 8009 | Postgres `social` | `post.published`, `post.failed` | `content.scheduled` |
| 11 | Scraper | 8010 | **MongoDB** | `scrape.completed`, `scrape.failed` | `scrape.requested` |
| 12 | Notification | 8011 | Postgres `notification` | `notification.sent` | `user.registered`, `invoice.paid`, `payment.failed`, `member.joined`, `post.published`, `post.failed`, `usage.threshold_reached` |
| 13 | Admin/Ops | 8012 | Postgres `admin` + Redis reads | — | **all** events (audit log) |
| — | Frontend | **3000** | — (consumes APIs) | — | — |

### Key flows

**Signup → account → starter credits** (no third-party key needed):
```
POST /auth/signup
  → Auth creates the user, calls User /internal/accounts/individual (sync)
  → User creates the individual Account, publishes account.created
  → Credits consumes account.created, grants the free-tier starter balance (once per account)
  → Notification consumes user.registered, emails the verification link
```
There is also an asynchronous healing path: Auth publishes `user.registered`,
which the User service consumes to provision the same account idempotently — so
a signup that happened while User was momentarily unreachable still gets its
account. Both paths share `provisioning.py` and the same `user_id` business key,
so whichever runs first wins and the other is a no-op.

**Buy credits (Stripe):** frontend → Stripe Checkout → Stripe webhook →
**Gateway** verifies the signature, dedups in Redis, relays to Billing →
Billing records the invoice and stages `invoice.paid` in its **outbox** → the
outbox poller publishes it → Credits grants the plan's credits (idempotent on
the Stripe invoice id).

**AI generation deducts credits (spec §10):** the AI service does **not** call a
debit endpoint. It publishes `ai.generation_completed` (with token counts) and
Credits, Usage, and Content all consume it off the fan-out. Credits derives the
charge from tokens and writes an append-only debit. A Credits outage can never
fail a generation the provider already billed — the event simply waits.

**Schedule → publish:** Content publishes `content.created` → Scheduler places
it on the account calendar and, when the publish time arrives (Celery Beat),
emits `content.scheduled` → Social publishes to LinkedIn and emits
`post.published` / `post.failed`.

### Identity, tenancy & roles

Every JWT carries `user_id` (`sub`), `account_id`, `role`, and `jti` (spec §6).
**All domain data is scoped by `account_id`, never `user_id`.** `account_id`
and `role` come from the User service, which Auth asks **synchronously** when
minting a token (`services/auth/user_client.py` → the User service's
`/internal` API) rather than from an event-fed read model — a projection would
be stale at the exact instant a token is minted, so a just-demoted member could
still receive an `admin` token. `/internal/*` is deliberately absent from the
Gateway route table, so it is reachable only inside the compose network.

**Platform SuperAdmin** (spec §8, Service 13) is a `users.is_superadmin` flag,
not an account role — it grants cross-account visibility through the Admin
service, not membership. It is designated by the `SUPERADMIN_EMAILS` env var and
reconciled on Auth startup in both directions (listed addresses are granted,
removed addresses are demoted and their sessions revoked). There is no promote
endpoint — deploy config is the only grant path.

### Credits ledger

The ledger (`credits.credits_ledger`) is **append-only** — every change is a
row, the balance is `SUM(amount)`, nothing is mutated in place. New accounts open
on `CREDITS_STARTER_GRANT` credits (its own `starter_grant` entry type, so the
history view never presents a gift as a purchase and refund claw-backs can't eat
it). Generation price is `ceil(total_tokens / 1000) * CREDITS_PER_1K_TOKENS`,
minimum 1 credit. Overspend is prevented up front by the AI service's
synchronous quota gate against Usage; the debit itself only ever *records* what
already happened, so an insufficient balance is allowed to go negative rather
than hide a debt.

---

## Local setup

### Prerequisites

- **Docker + Docker Compose v2** (`docker compose version`). This is the only
  hard requirement to run the stack.
- **OpenSSL** — to generate the RS256 JWT keypair (one-time).
- For local dev *outside* Docker (optional): **Python 3.12** and **Node 20+**
  (to run tests / lint / the Vite dev server directly).

### 1. Clone and generate the JWT keypair

The public key is committed; the **private key is gitignored** and must be
generated locally (a fresh clone will not have it):

```bash
openssl genrsa -out keys/jwt_private.pem 2048
openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem
```

### 2. Create your `.env`

```bash
cp .env.example .env
```

The stack **boots and the core signup → verify → login → starter-credits flow
works with the defaults in `.env.example`** (no paid keys needed) because of the
dev token affordances described below. Fill in third-party keys only for the
features that need them — see the next section.

### 3. Bring it up

```bash
docker compose up --build -d
```

Compose starts the infra (Postgres, Redis, RabbitMQ, MongoDB) with healthchecks,
then the 13 services, the Celery workers/beats, the Gateway, and the frontend.
First boot builds images and can take a few minutes. Check status with:

```bash
docker compose ps
```

All services should read `healthy`. The frontend is served at
**http://localhost:3000**, the Gateway at **http://localhost:8080**, and the
RabbitMQ management UI at **http://localhost:15672** (user/pass from `.env`).

---

## Environment variables — every key & how to get it

Grouped as they appear in `.env.example`. "Default OK" means the committed
default works for local Docker and needs no change.

### Infrastructure (default OK for local)

| Key | Purpose |
|-----|---------|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Postgres credentials (one instance, schema-per-service). |
| `REDIS_URL` | Redis connection (cache, JWT jti sessions, rate-limit counters, SSE pub/sub). |
| `RABBITMQ_USER`, `RABBITMQ_PASS`, `RABBITMQ_URL` | RabbitMQ credentials + AMQP URL. |
| `MONGO_USER`, `MONGO_PASS`, `MONGO_URL` | MongoDB (scraper documents). |

### JWT / auth (generate the keypair; the rest default OK)

| Key | Purpose · how to obtain |
|-----|-------------------------|
| `JWT_PRIVATE_KEY_PATH`, `JWT_PUBLIC_KEY_PATH` | Paths inside the container (`/keys/...`). The files come from the OpenSSL step above. |
| `JWT_ISSUER` | `iss` claim. Default OK. |
| `JWT_ACCESS_TTL_SECONDS`, `JWT_REFRESH_TTL_SECONDS` | Token lifetimes. Default OK. |
| `SUPERADMIN_EMAILS` | Comma-separated emails granted the platform SuperAdmin role on Auth startup. Empty = no SuperAdmin. Put **your** login email here to see the admin console. |

### Credits pricing (default OK)

| Key | Purpose |
|-----|---------|
| `CREDITS_PER_1K_TOKENS` | Price of a generation: `ceil(tokens/1000) * this`, min 1. |
| `CREDITS_STARTER_GRANT` | Free-tier welcome balance granted once per account (default 100; `0` disables the free tier). |

### Third-party keys (needed only for the matching feature)

| Key | Feature it unlocks · where to get it |
|-----|--------------------------------------|
| `OPENROUTER_API_KEY` | **AI text generation.** Sign up at [openrouter.ai](https://openrouter.ai) → *Keys*. Free/credit-based models available. Without it, generation calls fail. |
| `STRIPE_SECRET_KEY` | **Buying credits.** Stripe **test-mode** secret key from [dashboard.stripe.com](https://dashboard.stripe.com) → *Developers → API keys* (starts `sk_test_`). |
| `STRIPE_WEBHOOK_SECRET` | Verifies incoming Stripe webhooks (`whsec_...`). From the Stripe dashboard webhook endpoint, or from `stripe listen` when using the Stripe CLI. Reused by the Gateway to verify the same signature it relays to Billing. |
| `RESEND_API_KEY` | **Real email delivery** (verification, receipts, invites). Free tier at [resend.com](https://resend.com) → *API Keys*. Without it, use the dev token affordance below. |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI` | **Publishing to LinkedIn.** Create a LinkedIn Developer app at [linkedin.com/developers](https://www.linkedin.com/developers) and add the *Sign In with LinkedIn using OpenID Connect* and *Share on LinkedIn* products. Each developer brings their own app (LinkedIn issues per-app access). LinkedIn app review can be slow — this is the most likely feature to remain unproven. |
| `SOCIAL_TOKEN_ENCRYPTION_KEY` | Fernet key encrypting LinkedIn tokens at rest. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `OPENROUTER_WEBHOOK_SECRET`, `LINKEDIN_WEBHOOK_SECRET` | Optional webhook signature secrets verified at the Gateway. LinkedIn falls back to `LINKEDIN_CLIENT_SECRET` if unset. |

### Scraper & Gateway tuning (default OK)

| Key | Purpose |
|-----|---------|
| `SCRAPER_USER_AGENT`, `SCRAPER_MIN_DELAY_SECONDS`, `SCRAPER_TIMEOUT_SECONDS` | Scraper politeness / timeouts. |
| `GATEWAY_CORS_ORIGINS` | Allowed browser origins (`*` for dev; set the frontend origin to narrow it). |
| `GATEWAY_RATE_LIMIT_PER_MINUTE`, `GATEWAY_ACCOUNT_RATE_LIMIT_PER_MINUTE`, `GATEWAY_RATE_LIMIT_WINDOW_SECONDS` | Redis sliding-window rate limits (per-IP and per-account; `0` disables a limit). |

### Frontend build arg

| Key | Purpose |
|-----|---------|
| `VITE_GATEWAY_URL` | Baked into the static bundle at build time — where the **browser** reaches the Gateway (the host-published port, e.g. `http://localhost:8080`), not the compose-internal DNS name. |

---

## Ports

| Host port | What |
|-----------|------|
| 3000 | Frontend (nginx-served React bundle) |
| 8080 | **API Gateway** — the one public entry point |
| 8001–8012 | Individual service APIs (direct dev access): auth 8001, user 8002, billing 8003, credits 8004, usage 8005, ai 8006, content 8007, scheduler 8008, social 8009, scraper 8010, notification 8011, admin 8012 |
| 5432 | PostgreSQL |
| 6379 | Redis |
| 5672 / 15672 | RabbitMQ AMQP / management UI |
| 27017 | MongoDB |

---

## First-run walkthrough

With the stack up and the `.env.example` defaults (no email/Stripe keys):

1. **Sign up** at http://localhost:3000 (or `POST http://localhost:8080/auth/signup`).
   Because `AUTH_EXPOSE_DEV_TOKENS=1`, the response includes a
   `dev_verification_token` and the frontend surfaces it, so you can verify
   without a configured mailbox.
2. **Verify** the email with that token → **log in**. You now hold an
   account-scoped JWT and your individual Account exists.
3. **Check your balance** — you start on `CREDITS_STARTER_GRANT` (100) credits,
   granted by the Credits service consuming `account.created`.
4. To see the **admin console**, put your email in `SUPERADMIN_EMAILS`, restart
   the Auth service, and log in again.
5. To exercise **AI generation**, add an `OPENROUTER_API_KEY`; to exercise
   **buying credits**, add Stripe test keys; to publish to **LinkedIn**, add a
   LinkedIn app. Each is independent.

---

## Event-reliability design

The spec (§7) and the Definition of Done (§10, "all inter-service events survive
a forced restart of a consumer without data loss or duplication") drive four
mechanisms, all implemented in `libs/creditflow_common` and used uniformly.

### 1. Transactional outbox (Billing)

"Commit the DB change, then publish to RabbitMQ" has an unfixable gap: a crash
(or a broker outage) between the commit and the publish loses the event forever.
Publishing *before* the commit is worse — a rolled-back transaction announces a
change that never happened. Billing (`services/billing/outbox.py`) closes the
gap by making it a local problem:

- `stage()` inserts the event as a **row** in `outbox_events` using the caller's
  session — no commit. The domain change and the event row commit in **one
  transaction**, so "state changed" and "event recorded" are inseparable, and a
  broker outage is irrelevant (the commit touches only Postgres).
- `publish_pending()` (driven by a poller) reads unpublished rows, publishes each
  with publisher confirms, and marks it published. Broker unreachable? It stops;
  the rows wait and the next tick retries.

Delivery is **at-least-once**: a crash after the broker confirmed but before the
mark commits re-publishes the row next tick — with the **same** `event_id` (the
outbox row id), which the consumer-side dedup makes harmless.

### 2. Publisher confirms + durable, persistent messages

`creditflow_common.rabbitmq.Publisher` declares **durable topic exchanges**,
enables **publisher confirms** (`confirm_delivery`), and sends **persistent**
messages (`delivery_mode=2`) with `mandatory=True` so an unroutable message
raises instead of vanishing. Every message carries a stable `event_id` header.

### 3. DLQ + bounded retry

`declare_with_dlx()` binds every consumer queue to a **dead-letter exchange**.
The shared `consume()` loop retries a failing handler up to `MAX_RETRIES` (5)
with exponential backoff, re-publishing with an incremented `x-retries` header;
once exhausted it nacks without requeue so the message dead-letters into
`<queue>.dlq` for inspection instead of hot-looping forever. Queues are
**pre-declared by producers**, so an event emitted before its consumer ever
started is waiting in a durable queue, not lost.

### 4. Idempotent consumers (`processed_events` + business key)

Two independent layers make at-least-once safe (`libs/creditflow_common/idempotency.py`):

- **`processed_events(event_id)`** — before applying an event, the consumer
  calls `already_processed(db, event_id)`, which inserts the id (PK/unique
  constraint makes it race-safe) or reports it was seen. Crucially, the
  `processed_events` row and the domain rows commit in the **same transaction**,
  so a redelivery can never half-apply. This dedupes **broker redelivery**.
- **A domain business key** — e.g. the Stripe invoice id on a purchase grant,
  the `job_id` on a generation debit, the `account_id` on the once-per-account
  starter grant. This dedupes a **producer re-emitting** the same fact under a
  fresh `event_id`.

---

## Restart-resilience test (§10)

`scripts/restart_resilience_test.sh` is an automated, assertion-driven proof of
the DoD claim that events survive a forced consumer restart without loss or
duplication. It uses the starter-grant flow, so it needs **no third-party keys**.

```bash
docker compose up --build -d          # stack must be running first
./scripts/restart_resilience_test.sh
```

What it does:

- **Phase 1 — data loss.** `docker compose stop credits`, then sign up a new
  user *while the Credits consumer is dead*. It asserts the `account.created`
  event is sitting in the durable `credits.account_events` queue (queue depth ≥
  1), restarts Credits, and asserts exactly **one** `starter_grant` row and the
  correct balance appear — nothing was lost during the outage.
- **Phase 2 — duplication.** It re-publishes the same `account.created` twice —
  once under a **fresh** `event_id` (a producer replay) and once under the
  **same** `event_id` (a broker redelivery) — and asserts the ledger *still*
  holds exactly one grant, and that `processed_events` recorded the redelivered
  id exactly once. This exercises both idempotency layers.

The script reads its verdict from Postgres (`credits.credits_ledger`,
`credits.processed_events`) and RabbitMQ queue depth — the source of truth, not
the logs. To do the same by hand, the manual equivalent is: `docker compose stop
credits` → sign up → `docker compose exec rabbitmq rabbitmqctl list_queues name
messages` (see the message waiting) → `docker compose start credits` → query
`credits.credits_ledger` and confirm one `starter_grant` row.

---

## Running the tests

Every service's unit tests run against **plain `pytest` with no infrastructure**
— no Postgres, Redis, or RabbitMQ required. Services use SQLite/fakeredis in
tests and call their event handlers directly (the broker is mocked), so the
suite is fast and hermetic.

Run one service's tests:

```bash
cd services/credits
PYTHONPATH=. pytest -q
```

Run all backend suites the way CI does (from the repo root):

```bash
for d in services/*/; do
  if compgen -G "${d}test_*.py" > /dev/null || compgen -G "${d}tests/*.py" > /dev/null; then
    ( cd "$d" && pip install -r requirements.txt >/dev/null 2>&1; PYTHONPATH=. pytest -q )
  fi
done
```

Lint locally exactly as CI does:

```bash
pip install ruff && ruff check services libs      # backend
cd frontend && npm ci && npm run lint && npm run build   # frontend
```

---

## Git & CI workflow

Per spec §9:

- **Branches:** `main` (production, protected), `dev` (integration, protected),
  `feature/<name>`, `fix/<name>`. All work happens on `feature/*` or `fix/*`,
  opened as PRs into `dev`. `dev → main` is a release PR.
- **Conventional Commits** (`feat:`, `fix:`, `chore:`, …).
- **CI must pass before merge** — the spec requires **lint, unit tests, and
  Docker build**. Three path-filtered GitHub Actions workflows enforce this:
  - **`backend-ci`** — a `lint` job (**ruff**, pinned to the same version as
    local, reading the committed `ruff.toml`) and a `test` job that runs every
    service's `pytest` suite.
  - **`frontend-ci`** — `npm ci` → **ESLint** (`npm run lint`) → `npm run build`.
  - **`build-push`** — on push to `main`, builds every service image and pushes
    it to GHCR (this is the "Docker build" gate, and the image source the deploy
    compose would pull).

**Honest caveats:**

- Spec §9 requires *"at least one review from the teammate"* and assumes a
  2-person team (Backend + Frontend). This was built solo, so PRs were
  self-merged after green CI. Worth a one-line note to a mentor.
- Spec §9's *"merges to `main` trigger the AWS deployment pipeline"* is
  bonus/stretch and is **not** wired — see below.

---

## Dev-only affordances & known gaps

These exist so the system can run end-to-end **without paid third-party keys or
a platform service-auth token**. Each is intentional, isolated, and safe to
remove/disable for a real deployment. None affect the test suite.

| Affordance | Where | What it does | For a real deployment |
|------------|-------|--------------|-----------------------|
| `AUTH_EXPOSE_DEV_TOKENS=1` | `docker-compose.yml`, `services/auth/routes.py` | Echoes the email **verification** and password-**reset** tokens in the API response so those flows work with no mail provider configured. The frontend reads them (`Signup.tsx`, `types.ts`). | Set to `0` (or remove) and configure `RESEND_API_KEY`. |
| `USER_EXPOSE_DEV_TOKENS=1` | `docker-compose.yml`, `services/user/routes.py` | Same idea for team **invite** tokens. | Set to `0` and rely on emailed invites. |
| `SOCIAL_CONTENT_TOKEN` | `docker-compose.yml`, `services/social/consumer.py` | Dev stopgap bearer the Social consumer uses to fetch authoritative post content from the Content service. **Unset, it degrades gracefully** to the title/`image_url` mirrored on the `content.scheduled` event. | Replace with the platform service-auth token once it exists (below). |
| Unauthenticated `/internal/*` | `services/user/internal.py`, `services/admin/clients.py`, social | Service-to-service seams (e.g. Auth asking User for `account_id`/`role`) have **no service-auth token yet**. Mitigated by being **absent from the Gateway route table** — reachable only inside the compose network. | Introduce a signed service-auth token and require it on `/internal/*`; no route shapes change. |

There are **no stale `TODO`/`FIXME` markers** in the codebase (verified). The
"placeholder-JWT" comments some consumers carry describe a **defensive fallback**
in the notification/admin read models, not a live shortcut — they are covered by
tests and left intact.

---

## AWS free-tier constraints & tradeoffs

AWS deployment (spec §5 bonus) is **done** — the full stack runs on a single
AWS EC2 instance via `docker compose up`, live at
[http://13.60.163.115:3000](http://13.60.163.115:3000). This section documents
the free-tier shape and the honest tradeoffs, as required by the DoD.

**Intended shape (single-box):** one always-on EC2 `t2.micro`/`t3.micro` (the
750 hrs/month free-tier instance) running the whole compose stack, with Postgres
optionally moved to **RDS** `db.t3.micro` (free tier: one instance, 20 GB).
Merges to `main` were meant to trigger this via the `build-push` images + a
compose pull on the box.

**Constraints that make this genuinely tight:**

- **1 vCPU / 1 GB RAM** on a micro instance cannot comfortably host 13 FastAPI
  services **plus** their Celery workers/beats **plus** Postgres, Redis,
  RabbitMQ, MongoDB, and nginx at once. Realistically you must add swap, shrink
  the resident set (fewer workers), and accept slow cold starts — or step up to
  a paid instance.
- **No free managed broker or document DB.** Amazon MQ (RabbitMQ) and
  DocumentDB (Mongo-compatible) are **not** free-tier, so both must run
  in-container on the same micro instance, competing for that 1 GB. A free
  alternative for Mongo is **Atlas M0** (512 MB, off-box).
- **RDS free tier is a single AZ, single instance** — no HA, and the 20 GB cap
  is fine for the schema-per-service single database but leaves no room for
  growth.
- **Everything shares one failure domain.** A reboot of the box is a full
  platform outage; there is no redundancy.

**The tradeoff, stated plainly:** co-locating broker, cache, and databases with
all services on one micro instance is not production-grade — it trades HA,
isolation, and headroom for staying inside the free tier. It is, however,
exactly the deployment the platform's reliability design is built to *survive*:
durable queues, the transactional outbox, and idempotent consumers mean a
single-box restart drops no events and duplicates none (proven by
`scripts/restart_resilience_test.sh`). Scaling up is then a matter of pulling
Postgres/Redis/RabbitMQ onto managed services and running the stateless services
on more than one node — no code change, because nothing assumes co-location.
