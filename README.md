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
- **CI:** each service has a path-filtered workflow (pytest for backend, build for frontend). Images are built and pushed to **GHCR**; the deploy compose pulls them.

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
