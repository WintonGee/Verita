# DESIGN.md — Metered API Billing

> **THIS IS A SKELETON.** During day 4, this gets compressed from the ~15k words across working docs (`DATA-MODEL.md`, `PIPELINE.md`, `API.md`, `THREATS.md`, `OPS-SCALING.md`) into ~2,000 words of tight prose. Each section's word budget is set below. Bullets are placeholders for sentences; numbers/specifics are the load-bearing parts and should survive compression intact.

---

## Intro (~80 words)

- Brief: 5k customers, 200/s sustained, 2k/s peak, ~500M events/month, monthly invoices, accuracy is contractual.
- Stack: Django + DRF + Postgres 16. Cron-driven background jobs. Two React+Vite+TS SPAs.
- Design goal: simpler system with strong correctness, where each scale ceiling is a known fix, not a rewrite.
- This doc covers 7 things: data model, idempotency/concurrency, aggregation, failure modes, threat model, trade-offs, what's next.

---

## 1 — Data model (~280 words)

- All money in `bigint amount_micro_cents` (1 unit = $1e-8). 8 decimals of USD; $92B headroom per row. Bigint chosen over `numeric` for arithmetic speed and zero-ambiguity unit conventions.
- UUIDs on customer-visible PKs (event, invoice, line_item, credit, api_key) to prevent enumeration. `bigserial` on internal-only tables (audit_log) to save 8 bytes per row at 500M-row scale.
- Tenant column on every billable table (`customer_id`) with `RESTRICT` FK. Soft delete via `status` on customer; nothing else is deleted except expired idempotency_keys.

**Key indexes** (excerpt from full table in source):
- `event.UNIQUE(request_id)` — the idempotency primitive.
- `event(customer_id, event_timestamp DESC)` — serves `GET /v1/usage` and per-customer aggregation.
- `event(event_timestamp) BRIN` — append-only-by-time means BRIN is ~1000× smaller than B-tree; aggregator full-hour scans.
- `usage_window.UNIQUE(customer_id, window_start)` — both correctness and UPSERT target.
- `invoice.UNIQUE(customer_id, period_start)` — one invoice per customer per month, even without the advisory lock.
- `audit_log(resource_type, resource_id, created_at DESC)` — "show me history of this invoice."

**At 10×** (5B events/month): range-partition `event` by `event_timestamp` monthly via `pg_partman`. Aggregator only touches the current partition. **At 100×**: cold partitions to ClickHouse; Postgres keeps 3 hot months for aggregation. Schema shape is preserved at both steps — each scale change is "add infra," not "redesign."

---

## 2 — Idempotency & concurrency (~310 words)

Five potential double-effect scenarios; how each is closed:

**Event ingestion replay**: `INSERT … ON CONFLICT(request_id) DO NOTHING`. Postgres's unique index serializes concurrent inserts; replays are silent no-ops. The dedup primitive lives in the schema, not the application.

**Aggregator double-run**: hourly cron-driven. `pg_try_advisory_lock('aggregator')` at the top → second instance exits immediately. Internal work is `INSERT … ON CONFLICT DO UPDATE WHERE usage_window.sealed_at IS NULL` — sealed windows are immune to overwrites. Re-running with same input rows produces the same output.

**Invoicer double-run**: monthly cron. `pg_advisory_xact_lock(hash('billing_' || customer_id))` per customer. Idempotency check (`SELECT … WHERE customer_id=? AND period_start=?`) inside the lock makes the second run a no-op. Backstop: `UNIQUE(customer_id, period_start)`.

**Webhook delivered 3×**: `WebhookDelivery.UNIQUE(delivery_id)` catches replays. Inner txn re-checks `invoice.status` and no-ops if already `paid`. Stripe-style HMAC over `timestamp.body` with ±5 min window rejects stolen signatures.

**Ops "issue credit" double-click**: `Idempotency-Key` header required; `UNIQUE(staff_id, key)` in `idempotency_key` table. Same key + different body → 422 conflict.

**The one honest correctness boundary**: ingest is intentionally lockless (hot path; 2k/s peak). Therefore, a tiny race exists where an event lands during the invoicer's seal step with `is_late=FALSE`. **Closure**: just before commit, the invoicer flips `is_late=TRUE` for any event in the period whose `ingested_at > txn_started_at`. Such events roll into next month's adjustment line. **Property**: per-invoice correctness is eventual; aggregate correctness across consecutive invoices is exact. Documented as the system's deliberate boundary.

Concurrency primitives summary: `pg_try_advisory_lock` (cron singleton), `pg_advisory_xact_lock` (per-customer serialization), `SELECT … FOR UPDATE` (line-item edits, credit application), UNIQUE constraints (all idempotency keys).

---

## 3 — Aggregation pipeline (~280 words)

**Flow**: `event` (append-only) → `usage_window` (recomputable until sealed) → `invoice` + `line_item` (immutable once issued; overrides via audited PATCH).

**What's recomputable, what's immutable**:
- `event`: source of truth, append-only, never UPDATE/DELETE in app code.
- `usage_window.units_consumed`: recomputable from `event` rows as long as `sealed_at IS NULL`.
- `invoice` line items: immutable after `issued`, mutable only via PATCH that writes an audit row in the same transaction.
- `audit_log`: hard-immutable (DB trigger + revoked grants on `app_role`).

**Seal boundary**: invoice issuance. A window's `sealed_at` is set in the same transaction that creates the invoice. Drift between events and windows is recomputable; drift between windows and an issued invoice is not, and is resolved by a credit on the next invoice (audited).

**Late events**: at ingest time, `is_late = (window already sealed?)` via a subquery on `usage_window`. Late events are not added to the sealed window; the next invoicer run sweeps `is_late=TRUE AND adjusted_at IS NULL` events into a `kind='adjustment'` line item. Customer is billed correctly across two consecutive invoices.

**Reconciliation jobs** (daily, read-only, alert-on-drift):
1. `event_sum_vs_window` — auto-recompute unsealed windows; alert on sealed-window drift.
2. `line_items_vs_window` — alert ops; never silently re-issue.
3. `invoice_total_vs_lines` — recompute the denormalized total.

These three queries are also the answer to "how do you debug a wrong invoice": drill down through the same comparisons, identify which layer drifted, fix at the right level (window recompute, or credit if sealed).

---

## 4 — Failure modes (~310 words)

Three things break first at production scale. Each has a measured trigger, a named fix, and a migration story.

**(a) Ingest write throughput**. **Trigger**: sustained event rate > 3k/sec. **Symptom**: `POST /v1/events` p99 climbs from 30ms → 250ms; `pg_stat_activity` shows lock-waits on the B-tree backing `UNIQUE(request_id)`. **Why**: page-level lock contention on a single shared index. **Fix at 10×** (20k/sec): write-aside buffer (Redis Stream or Kafka). Events land in the buffer in 200µs; a flusher batches into Postgres via `COPY` at 5k rows/chunk. Idempotency proof unchanged because the buffer is keyed by `request_id` (SETNX). Customer-facing contract moves from `200 OK` (ingested) to `202 Accepted` (received). **Migration**: dual-write for a week → cut reader over → deprecate direct path. Scales with a known fix.

**(b) Aggregator scan time**. **Trigger**: `event` table > 1B rows (~2 months of sustained 200/s). **Symptom**: hourly cron exceeds 30 min; window staleness > 1 hour breaks dashboard accuracy. **Fix**: monthly range-partition on `event_timestamp` via `pg_partman`. Aggregator touches only the current partition (~50M rows, ~5GB index). **At 100×**: shovel cold partitions to ClickHouse; Postgres keeps 3 hot months. Schema/query shape preserved. Scales with a known fix.

**(c) Monthly invoicer wall-clock**. **Trigger**: customer count × per-customer time > scheduled window. At 5k × 600ms = 50min. **At 50k**: 8 hours, overlapping next-day aggregator. **Fix**: parallelize via `SELECT customer_id … FOR UPDATE SKIP LOCKED LIMIT 1` claim pattern. Add `last_invoiced_at` column on `customer`. N workers → N× speedup. Celery becomes worth introducing at this scale; before then it's overhead. Scales with a known fix.

Each fix is shape-preserving. The data model and idempotency proofs survive every step.

---

## 5 — Threat model (~340 words)

Three actors with concrete abuse scenarios. The full table is in `THREATS.md`; key entries summarized here.

**Hostile customer** (valid API key):
- *Cross-tenant read by UUID*: blocked at `CustomerScopedManager.for_customer()`, queryset-level filter on every billable model. Returns 404 (not 403) to avoid confirming resource existence. Test: `test_invoice_404_for_other_tenant`.
- *Event replay*: silent dedup via `UNIQUE(request_id)`.
- *Negative units to deflate own bill*: `CHECK (units_consumed >= 0)`.
- *Forged webhook*: HMAC-SHA256 over `timestamp.body` with env-only secret. Customer doesn't have the secret. Timestamp window of ±5min rejects captured-old replays.
- *Cross-tenant via cursor token*: cursor decodes through the same scoped manager. Forged cursor pointing at another tenant returns empty.

**Hostile internal user** (valid ops creds — the most dangerous because of intentional write access):
- *Large credit to a friend*: cannot be prevented; audit row written in the same transaction as the credit captures actor, IP, before/after, reason (required ≥10 chars). Daily report alerts on credits > threshold. Aggregate-by-actor weekly report surfaces patterns even when individuals stay under threshold.
- *Tamper with audit log*: Postgres trigger `BEFORE UPDATE OR DELETE ON audit_log → RAISE EXCEPTION`. Belt-and-suspenders: `app_role` has only INSERT/SELECT grants on `audit_log`; migrations run as a separate `migrator_role`. SQL-injected app code cannot mutate audit rows.
- *Manual mark-paid*: no ops endpoint exists for this transition. Only the webhook handler does `issued → paid`. Trade-off: no ops escape hatch; we accept this for audit clarity.

**Compromised webhook source** (HMAC secret leaked):
- Worst case: attacker marks arbitrary `issued` invoices as `paid`.
- Containment: webhook handler is the *only* path that does that transition. Blast radius is auditable via `webhook_delivery` rows + audit log.
- Cross-checked nightly: count of accepted webhooks vs processor's outbound count. Drift = compromise.
- Rotation: env-based, with `WEBHOOK_SECRET_CURRENT` + `WEBHOOK_SECRET_PREVIOUS` overlap window.

**Secrets policy**: keys in env only; `.gitignore` covers `.env*`; pre-commit hook scans for high-entropy strings; log scrubber redacts `Authorization` headers. argon2id for customer passwords; SHA-256 + per-key salt for API keys (high-entropy random secrets; bcrypt's slow-by-design property is wasted here).

---

## 6 — Trade-offs (~250 words)

**Trade-off 1: Cron + advisory locks over Celery**. Chose Django management commands invoked by system cron, each guarded by `pg_try_advisory_lock`. Alternative: Celery + Beat + Redis broker. **Why we picked cron**: the brief literally says "simple cron + a locked job table is fine." Our idempotency proofs already make retries naturally absorbable — failed aggregator runs are no-ops on next tick; failed invoicer runs are caught by the daily stuck-draft reconciliation job. Celery's value is queue visibility and retry tooling, neither of which we need at this scale. Redis is one less service to debug. **What we lose**: no built-in retry telemetry; we'd add Celery at 50k+ customers when parallel per-customer dispatch starts to matter.

**Trade-off 2: SHA-256 + salt over bcrypt for API keys**. API keys are 32-char base62 random strings (≥190 bits entropy). bcrypt's slow-by-design property defends against guessable passwords, which API keys aren't. The hot path is every API request; a millisecond bcrypt verification multiplied by 2k req/sec is a real CPU cost. **Trade-off**: a Postgres dump exposes hashes that can theoretically be brute-forced; in practice, 190-bit entropy makes this infeasible regardless of hash function.

**Trade-off 3 (honest correctness boundary)**: ingest is lockless. The invoicer absorbs the resulting race via the Step 10 sweep. **Alternative**: shared advisory lock on ingest, exclusive on invoicer. **Why we chose the lockless path**: forcing every ingest to take a per-customer lock adds ~0.5ms to the hot path; at 2k/s peak that's wasted under our actual race rates (invoicer runs once a month). The eventual-correctness property in aggregate is strong enough.

---

## 7 — What I didn't build, and would build next (~120 words)

Named because the absence is deliberate, not oversight.

- **Plan versioning**: today, a customer has one current plan. Adjustment line items use the customer's *current* marginal rate, not the original-period rate. Production requires versioning.
- **Per-endpoint pricing**: schema retains `endpoint` for analytics; pricing is aggregate-units. Per-endpoint would be a junction table.
- **Refund accounting**: refunds go through credits; no reversal accounting model.
- **Customer-facing dispute flow**: ops resolves disputes; no self-serve state machine.
- **Per-staff RBAC**: today all `is_staff` users are full-access. Django Groups would split `billing-readonly` vs `billing-admin`.
- **MFA / SSO** for ops staff.
- **Multi-region, DR, multi-currency, email invoicing, PDF rendering, dunning**.
- **Observability stack**: hooks for Prometheus + OpenTelemetry are described; the dashboards themselves are not built.

Each item is sized small relative to the core; none would change the data model.

---

## Word budget vs reality

| Section | Budget | Hard line on cut/keep |
|---|---|---|
| Intro | 80 | keep |
| Data model | 280 | keep specifics; drop index list if tight |
| Idempotency & concurrency | 310 | keep the five scenarios; can compress the boundary discussion |
| Aggregation pipeline | 280 | keep recomputable/immutable table; can drop reconciliation list |
| Failure modes | 310 | keep three triggers + numbers + fixes |
| Threat model | 340 | keep three actors' top entries; drop secondary scenarios |
| Trade-offs | 250 | keep three trade-offs; honesty matters more than length |
| Future work | 120 | bullet list only |
| **Total** | **1,970** | inside 1,500–2,500 band, with headroom |

Numbers, indexes, lock primitives, and the one honest correctness boundary are load-bearing — they prove specific claims the rubric asks about. Adjective-heavy prose is the first thing to cut on day 4.
