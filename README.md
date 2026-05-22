# Verita — Metered API Billing

Ingest usage events → aggregate into hourly windows → roll into monthly invoices
against a tiered price plan, with a payment webhook, a customer dashboard, and
an internal ops console. Built for the take-home brief.

- **[DESIGN.md](DESIGN.md)** — the design write-up (start here; it's half the deliverable).
- **[GUIDE.md](GUIDE.md)** — how to *use* the running system (API, dashboards, ops cookbook).

Stack: Django 5 + DRF + Postgres 16, cron-driven background jobs, two
React + Vite + TypeScript SPAs. Money is integer micro-cents throughout
(1 unit = $1e-8).

## Prerequisites

- Docker + Docker Compose
- Node 18+ (for the two front-ends, run via Vite)

## Quick start

```bash
# 1. Config (dev defaults are fine; nothing secret is committed)
cp .env.example .env

# 2. Bring up the whole stack: Postgres + Django API + cron + both SPAs.
#    First boot creates the Postgres role split (migrator_role / app_role) and
#    the backend auto-applies migrations. --wait blocks until everything is
#    healthy, so the seed below can't race the migration.
#    (First run builds images — give it a couple minutes.)
docker compose up -d --wait

# 3. Seed realistic data: customers, API keys, ~thousands of events
#    (with late arrivals + duplicate request_ids), plus a demo ops user.
docker compose run --rm backend python manage.py seed --customers=5 --days=45

# 4. Run the pipeline by hand (cron does this on schedule):
docker compose run --rm backend python manage.py aggregate_events --catch-up
docker compose run --rm backend python manage.py issue_invoices
```

That's it — open the dashboards (URLs + logins below). The SPAs are served by
`docker compose` (Vite dev servers, ports 5173 / 5174). For frontend development
with hot reload you can instead run a SPA directly:
`cd frontend/customer-web && npm install && npm run dev`.

The seed prints customer logins and **plaintext API keys** (shown once). Defaults:

| Surface | URL | Login |
|---|---|---|
| Customer dashboard | http://localhost:5173 | `user1@example.com` / `password123` |
| Ops console | http://localhost:5174 | `ops` / `ops-pass-123` |
| API | http://localhost:8000 | API key `vk_live_…` (from seed output) |
| API docs (OpenAPI/Swagger) | http://localhost:8000/api/docs/ | — |

> Each SPA's Vite server proxies its API paths to the backend (customer-web →
> `/v1` + `/api`, ops-web → `/ops` + `/api`), so cookies and CSRF work
> same-origin. In compose the proxy targets `backend:8000`; run directly it
> targets `localhost:8000` (`VITE_PROXY_TARGET`). The brief calls for minimal,
> functional front-ends — that's what these are.

## Running the tests

```bash
docker compose run --rm backend python -m pytest          # all 117
docker compose run --rm backend python -m pytest -m concurrency   # the thread-race tests
```

Tests are concentrated on the correctness boundaries the brief calls out:
idempotency, concurrency, tenant isolation, reconciliation, money-moving
actions. Highlights: 20-thread concurrent ingest → one row; 8-thread invoice
issuance → one invoice; 6-thread credit issuance → one credit; webhook
delivered 3× → one payment; audit-log UPDATE/DELETE blocked at the DB.

## Try the API directly

```bash
KEY=vk_live_...   # from the seed output

# Idempotent batched ingest (note the duplicate request_id → "duplicate")
curl -s -X POST http://localhost:8000/v1/events \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"events":[
        {"request_id":"r1","endpoint":"/x","units_consumed":5,"timestamp":"2026-05-20T10:00:00Z"},
        {"request_id":"r1","endpoint":"/x","units_consumed":5,"timestamp":"2026-05-20T10:00:00Z"}]}'
```

The payment webhook is HMAC-signed; see `backend/tests/test_webhook.py` for the
exact signing scheme (`X-Signature: t=<ts>,v1=<hex>` over `"{ts}.{body}"`).

## Background jobs

System cron (the `cron` compose service) runs these; each is also a management
command you can invoke directly. Each takes a `pg_try_advisory_lock` so a
second concurrent run is a no-op.

| Command | Schedule | Purpose |
|---|---|---|
| `aggregate_events [--catch-up]` | hourly :15 | events → usage_windows (idempotent UPSERT) |
| `issue_invoices` | 1st of month 00:30 | usage_windows → invoices for the previous month |
| `run_reconciliation` | daily 03:00 | drift detectors (window vs events, total vs lines, stuck drafts) |
| `cleanup_idempotency_keys` | daily 04:00 | prune the one table where deletion is allowed |
| `seed --customers --days [--reset]` | manual | generate demo data |

## Layout

```
backend/            Django project
  apps/tenancy/     customer, customer_user, api_key, customer_session
  apps/billing/     price_plan/tier, event, usage_window, invoice, line_item,
                    credit; aggregator / invoicer / pricing / reconciliation
  apps/audit/       audit_log (immutable), webhook_delivery, idempotency_key
  apps/api/         DRF auth, /v1 (customer), /ops (staff), /webhooks
  tests/            117 tests, organized by correctness boundary
frontend/customer-web/   dashboard SPA (usage chart, invoices)
frontend/ops-web/        ops console SPA (customers, credit + override modals)
ops/postgres/init/       role-split bootstrap (runs on first DB boot)
docs/design-notes/       detailed design notes behind DESIGN.md
DESIGN.md                the write-up (read this)
```

## Notes on a few deliberate choices

- **Audit immutability is enforced at the database**, two ways: a trigger that
  raises on UPDATE/DELETE, and `REVOKE UPDATE, DELETE … FROM app_role`. The app
  connects as `app_role`; migrations run as `migrator_role`. See
  `ops/postgres/init/01-roles.sh` and `apps/audit/migrations/0003_*`.
- **Tenant scoping** lives in a manager whose default `.objects` query *raises*;
  views must use `.for_customer()`. A meta-test pins the `.unsafe_all_tenants()`
  allowlist.
- **Cron + advisory locks over Celery** — the brief allows it and idempotency
  makes retries free. The trade-offs (and where this breaks first at scale) are
  in DESIGN.md §4 and §6.

See DESIGN.md for the data model, idempotency/concurrency proofs, failure modes,
threat model, trade-offs, and what's intentionally not built.
