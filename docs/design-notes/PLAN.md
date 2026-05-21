# Metered API Billing — Working Plan

Take-home for Verita. ~4 days. AI assistance encouraged.

## Deliverables

1. Git repo runnable locally via `docker compose up`
2. Seed/generator script producing realistic data
3. `DESIGN.md` — 1,500–2,500 words, counts as much as the code

## Domain summary

- **Customer** → has one or more **API keys**
- **Event** (request_id, customer_id, api_key_id, endpoint, units, timestamp) emitted per API call (we simulate via a generator)
- **Usage window** — one row per customer × hour, rolled up by a scheduled job
- **Invoice** with **line items** — monthly, computed from windows against a tiered **price plan** (e.g. 0–10k free, 10k–100k at $0.001/unit, beyond at $0.0005/unit)
- **Payment webhook** — mock, signed, idempotent
- **Credits**, **line-item overrides** — ops actions with immutable **audit log**
- **Anomaly signals** — e.g. usage 10× a customer's 30-day average

## APIs (minimum)

### Customer-facing (`/v1`)
- `POST /v1/events` — batched ingestion, idempotent on `request_id`
- `GET /v1/usage` — paginated, filterable by date range and api key
- `GET /v1/invoices`, `GET /v1/invoices/{id}`

### Ops-facing (`/ops`)
- `GET /ops/customers`, `GET /ops/customers/{id}`
- `POST /ops/customers/{id}/credits`
- `PATCH /ops/invoices/{id}/line-items/{id}` — override with audit
- `POST /webhooks/payments` — signed, replay-safe

## Security must-haves

- Every `/v1` endpoint scoped to authenticated customer; scoping at middleware/manager layer (NOT in each view).
- API keys: never retrievable in plaintext after creation. Store hash + lookup-prefix.
- Webhook: HMAC signature verified against env secret, replay-safe via stored delivery IDs.
- Audit log: immutable at the DB layer (trigger that blocks UPDATE/DELETE, or revoked grants).
- No secrets in repo; all from env.

## Front-ends (minimal, functional)

- **Customer dashboard**: current-period usage chart, invoice list, invoice detail
- **Ops console**: customer list, customer detail (usage + invoices), credit issuance, line-item override
- Brief: "We are evaluating operational UX clarity and safety, not frontend polish."

## Production target (design for)

- 5,000 active customers
- 200 events/sec sustained, 2,000/sec peak
- ~500M events/month
- Monthly invoices, accuracy is contractual
- Single-node Postgres is acceptable; explain where it breaks and the migration path

## Stack constraints

- Real relational DB
- Money stored in **integer minor units**
- Background job mechanism (cron + locked job table is acceptable)
- Tests focused on: idempotency, concurrency, tenant isolation, reconciliation, money-moving actions

## Scoring rubric (weight, what "strong" looks like)

| Category | % | Strong-looking work |
|---|---|---|
| Data model & integrity | 18 | Right keys/FKs, integer money, indexes match real queries, constraints not comments |
| Concurrency & correctness | 18 | Provably idempotent aggregator/webhook; no double-credit; ingest handles replays |
| Scaling reasoning (DESIGN.md) | 13 | Specific numbers; identifies what breaks first; "won't scale" vs "fixable" |
| API & frontend craft | 13 | Sane REST, pagination reasoning, money-moving UI has confirm + idempotency token, loading/error states |
| Security & isolation | 10 | Scoping at right layer; key hashing; immutable audit; specific (not OWASP-generic) threat model |
| Trade-off writeup | 10 | Real alternatives considered, honest reflection |
| Operational thinking | 10 | Alerting hooks; migration story; how ops debugs a wrong invoice |
| Code quality & testing | 8 | Readable; tests cover what would break |

## DESIGN.md required sections

1. **Data model** — schema, indexes, why those indexes, what to add at 10× and 100×
2. **Idempotency & concurrency** — replay scenarios for ingest, aggregator, webhook, ops credit/override; locking/dedupe
3. **Aggregation pipeline** — events → windows → line items, recomputable vs immutable, drift reconciliation
4. **Failure modes** — three things that break first at scale, with fix
5. **Threat model** — hostile customer, hostile internal user, compromised webhook; concrete abuse scenarios (cross-tenant, replay, operator misuse, invoice tampering, credential leak, duplicate financial actions)
6. **Trade-offs** — ≥2 non-obvious decisions, the rejected alternative, why
7. **What I didn't build / would build next**

## Architectural decisions (locked)

| Decision | Choice | Notes |
|---|---|---|
| Backend stack | **Django + DRF + Postgres** | Matches Verita's stack; ORM managers give us tenant scoping at a layer that can't be forgotten |
| Postgres version | **16** | RLS, advisory locks, `SKIP LOCKED`, generated columns all available |
| Background jobs | **System cron + Django management commands + `pg_advisory_lock`** | Brief explicitly says "simple cron + a locked job table is fine." Drops Redis + Celery from the stack (one fewer service, one fewer thing to debug). Each scheduled task is `python manage.py <task>`; the command takes a `pg_try_advisory_lock` and exits early if held. Same per-customer xact lock pattern inside the work. Trade-off documented in DESIGN.md §6: no built-in retry/visibility like Celery; we accept this because (a) hourly aggregator re-runs naturally absorb failures, (b) monthly invoicer failures are flagged by the daily stuck-draft reconciliation job |
| Frontend | **Two separate SPAs**, `apps/customer-web` and `apps/ops-web`, Vite + React + TS | Cleaner auth separation; brief explicitly allows this |
| Tenant scoping | **DRF base permission + Django manager** — `CustomerScopedManager` filters `objects` by `request.customer` set in middleware; views inherit from a base that wires it up | Considered Postgres RLS; rejecting because Django's connection-per-request model makes RLS session vars awkward and the test story is harder. Will document this trade-off in DESIGN.md |
| API key storage | **SHA-256 + per-key salt, stored with a short lookup prefix** | Key shape: `vk_live_<8-char prefix>_<random 32 chars>`. Index on prefix, verify hash. Keys are high-entropy so bcrypt is overkill — but we'll mention it in DESIGN.md as a considered alternative |
| Idempotency key for POST mutations | **Client-supplied `Idempotency-Key` header**, stored with response body hash in a dedicated table, scoped per customer | Standard Stripe-style pattern |
| Webhook crypto | **HMAC-SHA256** over `timestamp.body`, headers `X-Signature` + `X-Timestamp`; reject if timestamp outside ±5 min; persisted `delivery_id` for replay dedupe | Mirrors Stripe webhook design |
| Money type | **bigint micro-cents** (1 unit = $1e-8) | Pricing is $0.0005/unit; need sub-cent precision. micro-cents = 6 decimal places of USD, fits comfortably in bigint. Considered numeric(18,6); chose bigint for speed and zero-ambiguity arithmetic. Stored as `amount_micro_cents` everywhere |
| Audit immutability | **Postgres trigger** that raises on UPDATE/DELETE of `audit_log` | Belt-and-suspenders: also revoke UPDATE/DELETE from the app role |

## Phased plan (4 days, ~28h budget on ~32h available)

> Time-checked against advisor review. **DESIGN.md skeleton lives on Day 1**, not Day 4 — it's worth ~50% of the grade and compression from 15k working-doc words to ~2,000 is a real task.

### Day 1 — Foundation (~8h)
- Repo skeleton: `backend/`, `frontend/customer-web/`, `frontend/ops-web/` directories
- `docker-compose.yml`: Postgres 16 + Django + a `cron` sidecar container that invokes `python manage.py <task>` on schedule
- **Postgres roles**: `migrator_role` (DDL) and `app_role` (DML, no UPDATE/DELETE on audit_log). DATABASE_URL in app points at `app_role`. Migration runs as `migrator_role`. **This is wired on Day 1 or the audit-immutability story is theatre.**
- Migrations: customers, customer_user, api_keys, events, usage_windows, invoices, line_items, credits, audit_log, webhook_deliveries, idempotency_key, price_plan, price_tier
- DB constraints (CHECK on units, money), unique indexes, immutable audit_log trigger
- API key auth middleware → request.customer
- `CustomerScopedManager` + `CustomerScopedViewSet`
- `POST /v1/events` with batch + idempotent ingest
- Tests: idempotency (single + concurrent), tenant isolation on ingest, audit trigger blocks UPDATE/DELETE
- **DESIGN.md skeleton** (headers + 2-sentence stubs per section) so compression isn't a day-4 surprise

### Day 2 — Pipeline (~8h)
- Hourly aggregator (`python manage.py aggregate_events`), idempotent UPSERT, global advisory-lock guard
- Monthly invoicer (`python manage.py issue_invoices`), per-customer xact lock, full sequence including Step 10 race-closure sweep
- Webhook handler: signature verify, timestamp window, delivery_id dedupe, amount-match check, status transition + audit
- Tiered pricing math + Hypothesis property tests
- Seed/generator script (`python manage.py seed --customers=10 --days=30` w/ late events + duplicates)
- Tests: aggregator double-run, invoicer double-run, webhook 3× replay, tier math at boundaries

### Day 3 — Surface (~8h)
- `GET /v1/usage` (cursor pagination), `GET /v1/invoices` (page+limit), `GET /v1/invoices/{id}`
- `/v1/auth/login`, `/v1/auth/logout`, `/v1/me`
- `GET /ops/customers`, `GET /ops/customers/{id}` (with anomaly query)
- `POST /ops/customers/{id}/credits` (Idempotency-Key required)
- `PATCH /ops/invoices/{id}/line-items/{id}` (audit in same txn)
- Tests: cross-tenant ID guessing → 404, idempotency-key reuse with different body → 422, double-credit attempt
- Reconciliation drift queries (read-only, behind a management command)

### Day 4 — Frontend + DESIGN.md (~6–8h)
- Customer dashboard: login, usage chart, invoice list, invoice detail
- Ops console: customer list, customer detail, issue-credit modal (with Idempotency-Key + confirmation), line-item override modal
- Loading + error states (not afterthoughts)
- **DESIGN.md compression**: take the 15k words across 8 working docs, distill to 1,500–2,500 words. Outline is set; this is editing, not writing.
- README setup notes

### Day 4 slack (~2h)
- The one thing that doesn't work the first time. (There will be one.)
- Polish, smoke test of `docker compose up` from a clean checkout

## Open questions / decisions still ahead

- **Ops auth model** — staff Django session + CSRF for the ops SPA? Or separate JWT? Lean: Django session auth with `is_staff` check + DRF `SessionAuthentication`. Customer SPA uses API key (or short-lived token derived from it for browser use)
- **Customer SPA auth** — customers need to view dashboards too; API key in browser is iffy. Options: (a) treat API keys as headless-only and add a separate "console session" cookie for the dashboard, (b) use API key directly in browser (simpler but worse hygiene). Lean: (a) — login flow + session cookie scoped to that customer, API keys remain server-to-server
- **Anomaly detection** — compute on read (cheap, ~30-day window query) or precompute into a `customer_baseline` table? Lean: compute on read for the take-home, mention precompute as a 10× scaling fix
- **Hourly window boundary** — UTC, no DST concerns. Aggregator processes "windows older than now()-30min" so late events have a grace period before sealing. Watermark/seal pattern
