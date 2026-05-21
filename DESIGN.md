# DESIGN.md — Metered API Billing

Django 5 + DRF + Postgres 16, two React/Vite SPAs, cron-driven background jobs.
Designed for the stated target — 5,000 customers, 200 events/s sustained
(2,000/s peak), ~500M events/month, monthly invoices where accuracy is
contractual — as a single-Postgres system with strong correctness and a clear
path to scale. Money is integer micro-cents throughout (1 unit = $1e-8).

**Pipeline at a glance.** Three scheduled jobs move data left-to-right. Each box
is a table; the line beneath it is that table's mutability rule.

```
   POST /v1/events         aggregate_events          issue_invoices
   (idempotent ingest)     (hourly cron)             (monthly cron)
          |                       |                         |
          v                       v                         v
     +---------+   rolls   +--------------+   rolls   +-------------------+
     |  event  | --------> | usage_window | --------> | invoice/line_item |
     +---------+           +--------------+           +-------------------+
     append-only           recomputable              immutable once issued;
     source of truth       until sealed              edit only via audited
                                                      line-item override

   payment: POST /webhooks/payments (signed, replay-safe) marks issued -> paid
```

## 1. Data model

The spine is `event → usage_window → invoice/line_item`, with `customer`,
`api_key`, `customer_user`, `price_plan/price_tier`, `credit`, `audit_log`,
`webhook_delivery`, and `idempotency_key` around it.

**Keys & money.** Customer-visible rows (`invoice`, `credit`, `api_key`,
`customer`) use UUID PKs so tenants can't enumerate neighbours by incrementing
an integer. Internal high-volume rows (`event`, `audit_log`) use `bigserial` —
8 bytes vs 16, which matters on a 500M-row table, and they're never exposed.
Every money column is `bigint` micro-cents; bigint's max ÷ 1e8 ≈ $92B per row,
ample for any invoice. Floats are used only for display formatting.

**Indexes match the queries actually run.** Each earns its place:
- `event UNIQUE(customer_id, request_id)` — the ingest idempotency primitive,
  scoped per-tenant (see §2/§5: prevents cross-tenant request_id poisoning).
- `event(customer_id, event_timestamp DESC)` — `GET /v1/usage` and per-customer
  aggregation.
- `event(api_key_id, event_timestamp DESC)` — `/v1/usage?api_key_id=`.
- `event(customer_id) WHERE is_late AND adjusted_at IS NULL` — partial index for
  the late-event sweep; tiny because almost no rows qualify.
- `usage_window UNIQUE(customer_id, window_start)` — correctness + UPSERT target.
- `usage_window(window_start) WHERE sealed_at IS NULL` — the aggregator's
  "what's still open" scan.
- `invoice UNIQUE(customer_id, period_start)` — one invoice per customer/month,
  and an idempotency backstop; `(status, period_end)` for the issuer.
- `audit_log(resource_type, resource_id, created_at DESC)` — "history of this
  invoice."

Constraints live in the schema, not in comments: `CHECK(units_consumed >= 0)`,
`CHECK(amount_micro_cents >= 0)` on invoices, `CHECK(credit.amount > 0)`,
tier `CHECK(end_unit > start_unit)`, and the uniqueness constraints above. FKs
are `RESTRICT` — you cannot delete a customer with invoices, or a plan with
live customers. Nothing is hard-deleted except expired `idempotency_key` rows.

**At 10× / 100×.** `event` and `usage_window` are the only hot tables. At ~5B
events/year, range-partition `event` by `event_timestamp` monthly (`pg_partman`)
so the aggregator and vacuum touch only the current partition; drop partitions
past retention. At 100×, move cold partitions to ClickHouse/BigQuery and keep
~3 hot months in Postgres for aggregation. Every other table stays small enough
to live on one node for years. These are shape-preserving changes — the schema
and the queries don't change, only where the bytes sit.

## 2. Idempotency & concurrency

Idempotency lives in the schema, not in application bookkeeping. Five replay
scenarios, each closed and tested:

**Event ingestion replayed.** `INSERT … ON CONFLICT(customer_id, request_id)
DO NOTHING` in one multi-row statement. Re-delivery is a silent no-op; the
response reports `accepted`/`duplicate` per request_id. A 20-thread test
hammering the same request_id leaves exactly one row — the unique index
serializes, no app lock. The dedup key is scoped to the tenant: request_ids are
globally-unique by client convention (UUIDs), but scoping the constraint stops
a hostile customer from suppressing another tenant's event by pre-claiming its
request_id (§5). (`tests/test_concurrency.py`, `tests/test_event_ingest.py`)

**Aggregator runs twice.** A global `pg_try_advisory_lock` makes a second
invocation return immediately. The work is an idempotent UPSERT recomputing the
full window sum, guarded by `WHERE usage_window.sealed_at IS NULL`, so sealed
windows are immutable and re-runs produce identical totals.
(`tests/test_aggregator.py`: `test_rerun_produces_same_total`, `test_sealed_window_is_not_recomputed`)

**Webhook delivered three times.** `webhook_delivery UNIQUE(delivery_id)`
catches replays; inside the transaction we re-check `invoice.status` and no-op
if already paid. Tested: three deliveries → one `issued→paid` transition, one
audit row, two no-ops. (`tests/test_webhook.py::test_replay_same_delivery_id_three_times`)

**Ops clicks "issue credit" twice.** A required `Idempotency-Key` header keys a
staff-scoped `idempotency_key` row that stores the response; a replay returns it
verbatim, and a reused key with a *different* body returns 409. The DB backstop
is `credit UNIQUE(customer_id, idempotency_key)`: a 6-thread concurrent test
produces exactly one credit and one audit row.
(`tests/test_ops.py`: `test_concurrent_credit_same_key_one_credit`, `test_issue_credit_double_click_is_idempotent`)

**Invoicer runs twice for a period.** Per-customer `pg_advisory_xact_lock` +
an existence check inside the lock + `UNIQUE(customer_id, period_start)` as a
hard backstop. An 8-thread concurrent test yields exactly one invoice.
(`tests/test_invoicer.py::test_concurrent_issuance_yields_one_invoice`)

Lock primitives: `pg_try_advisory_lock` (cron singletons), `pg_advisory_xact_lock`
(per-customer serialization, auto-released on commit), `SELECT … FOR UPDATE`
(line-item edits, window reads), and unique constraints (every dedup key).

**The one honest correctness boundary.** Ingestion is intentionally lockless —
the hot path can't afford a per-customer lock at 2,000/s. So an event can land
during the invoicer's seal step with `is_late=false` even though its window is
about to seal. The invoicer closes this in-transaction (step 10): just before
commit it flips `is_late=true` for any event in the period with
`ingested_at > txn_started_at`. Those become next month's adjustment line.
Per-invoice correctness is therefore *eventual*; aggregate correctness across
consecutive invoices is *exact*. This is the deliberate trade and it's
documented where it lives.

## 3. Aggregation pipeline

`event` (append-only, source of truth) → `usage_window` (recomputable until
sealed) → `invoice`/`line_item` (immutable once issued; edited only via an
audited override). The hourly aggregator UPSERTs window sums from events. The
monthly invoicer, per customer in one transaction: takes the per-customer lock,
returns early if already issued, **re-aggregates the period** (so windows
reflect every committed event), computes tiered line items, folds in
prior-period late events as an adjustment, applies pending credits (flooring
the total at $0), marks the invoice issued, **seals** the period's windows, runs
the race-closure sweep, and writes an audit row.

**What's recomputable vs immutable.** Window totals are recomputable from events
while `sealed_at IS NULL`. Issuance is the seal boundary: afterward the invoice
is immutable except via the audited line-item override. The audit log is
hard-immutable (below).

**Late events & drift.** An event arriving after its window is sealed is flagged
`is_late` at ingest (a subquery checks the window's seal state) and swept into
the next invoice as a `kind='adjustment'` line at the customer's current
marginal rate. Drift is caught by three read-only daily reconciliation checks:
event-sum vs window total (paging if the window is sealed), invoice total vs
line-item sum, and stuck-draft detection (`tests/test_reconciliation.py`).
These three queries are also the
"debug a wrong invoice" runbook — walk them top-down to localize where a number
diverged, then correct with a credit (never a silent edit to a sealed invoice).

## 4. Failure modes

Three things break first at scale; each is "scales with a known fix," not "won't
scale."

**Ingest write throughput**, ~3,000 events/s sustained. Symptom: `POST
/v1/events` p99 climbs as page-level lock contention builds on the
`UNIQUE(request_id)` B-tree. Fix at 10× (20k/s): a write-aside buffer (Redis
Stream/Kafka) keyed by request_id, flushed to Postgres in `COPY` batches; the
idempotency proof is unchanged and the customer contract moves from `200` to
`202 Accepted`.

**Aggregator scan time**, ~1B rows in `event`. The hourly scan over a 50GB index
exceeds the cron interval and windows go stale. Fix: monthly range partitioning
so each run touches ~50M rows; at 100×, cold partitions to an OLAP store.

**Monthly invoicer wall-clock.** 5,000 customers × ~600ms ≈ 50 min — fine
monthly, but 50k customers ≈ 8 h and overlaps the aggregator. Fix: parallel
per-customer dispatch via `SELECT … FOR UPDATE SKIP LOCKED` over a claim queue,
N workers → N×. This is the point where Celery earns its place; before it,
cron + advisory locks is simpler and sufficient.

**Where these numbers come from** (back-of-envelope, not yet load-tested): each
ingest insert touches the `UNIQUE(customer_id, request_id)` B-tree plus two
secondary indexes (~3 index writes/event); at 3k/s that's ~9k index-IOPS, which
is roughly where a single commodity-RDS writer's IOPS + WAL saturate — hence the
~3k/s ceiling. The ~600ms/customer invoicer cost is one read of a month of
windows (≤744 rows/customer) + tiered compute + a handful of inserts in one
transaction. Turning these estimates into measured numbers (drive the seed
generator + time it) is a named follow-up.

**Observability — what we'd alert on.** Prometheus counters on the hot paths,
structured logs with a `trace_id`, and the daily reconciliation as the backstop.
The page-vs-warn split:

| Signal | Threshold | Sev | Why |
|---|---|---|---|
| `invoicer.failed_run` | any | **page** | a financial cron failed |
| `drift.window` (sealed) | > 0.5% | **page** | a sealed-window invariant broke |
| `aggregator.failed_runs` | ≥ 3 in a row | **page** | pipeline is broken |
| `webhook.invalid_signature_rate` | > 5%/hr | **page** | attack or botched secret rotation |
| `event.ingest_5xx` | > 0.1%/min | **page** | data plane breaking |
| `aggregator.lag` | > 2 h | warn | dashboard data going stale |
| `idempotency_key.collision_with_diff_body` | any | warn | a client bug worth surfacing |

(Fuller table + the "debug a wrong invoice" runbook in `docs/design-notes/OPS-SCALING.md`.)

## 5. Threat model

**Hostile customer (valid API key).** Cross-tenant reads are blocked at the
`CustomerScopedManager` — a bare `.objects` query *raises*; only `.for_customer()`
returns rows, and a guessed UUID for another tenant returns 404, not 403, so
existence isn't confirmed. The `customer_id` is taken from the authenticated key,
never from request input. Replays dedup on `(customer_id, request_id)` — scoped
per tenant so A can't suppress B's events by pre-claiming a request_id; negative
units hit a CHECK; a forged webhook fails HMAC; brute-forcing a 190-bit key or the
rate-limited login (5/min/IP) is infeasible.
(`tests/test_tenant_scoping.py`, `tests/test_unsafe_allowlist.py`)

**Hostile insider (valid staff).** Can't be prevented, so it's made undeniable:
every credit and override writes an `audit_log` row — actor, IP, before/after,
required reason — in the same transaction as the effect. The audit log is
immutable at the database, two ways: a `BEFORE UPDATE OR DELETE` trigger that
raises for *any* role, and `REVOKE UPDATE, DELETE … FROM app_role`. The app
connects as `app_role`; migrations run as a separate `migrator_role`. A
SQL-injected app connection still cannot alter history. There is deliberately no
ops endpoint to mark an invoice paid — only the signed webhook can — so an
insider can't fake payment without leaving the audit trail of a credit/override.
(`tests/test_audit_immutability.py`, `tests/test_role_split.py`)

**Compromised webhook source (leaked HMAC secret).** Blast radius is one
transition (`issued→paid`); the payload amount is verified against the invoice
total and never used to mutate it. The `X-Timestamp` ±5-min window stops replay
of captured signatures; rotation is supported via current+previous secrets. A
daily count of accepted deliveries vs the processor's outbound count surfaces
abuse.

**Credentials.** API keys are stored as SHA-256(salt‖secret) + a lookup prefix,
shown once at creation, never recoverable. Customer passwords use argon2id;
session cookies store only a token hash. All secrets are env-based; none in the
repo.

## 6. Trade-offs

**Cron + advisory locks over Celery.** The brief allows "cron + a locked job
table," and our idempotency guarantees make retries free — a failed aggregator
run is absorbed by the next tick, a failed invoicer run is caught by the
stuck-draft reconciliation check. Celery would add a broker and a worker tier to
debug for visibility we don't yet need; it becomes worth it at the 50k-customer
parallel-invoicer point, and the migration is additive.

**SHA-256 over bcrypt for API keys.** API keys are 190-bit random secrets, not
human passwords, so bcrypt's deliberate slowness defends against nothing here
while taxing every request's auth lookup. We keep argon2id for customer
*passwords*, which are guessable. (A DB dump exposes only hashes either way.)

**Lockless ingest + invoicer sweep over locked ingest.** Forcing a per-customer
lock on every event would make ingest correct-by-construction but cost ~0.5ms on
the 2,000/s hot path to defend against a race that only opens once a month at
seal time. We took the lockless path and absorb the race in the invoicer,
accepting eventual per-invoice / exact-aggregate correctness.

**Front-ends in compose as Vite dev servers, not nginx-static.** `docker compose
up` brings up everything including both SPAs, each a Vite dev server whose proxy
forwards its API paths to `backend:8000` (cookies + CSRF stay same-origin). The
rejected alternative — building the SPAs and serving them behind nginx — means
proxying Django's CSRF-protected session through nginx with the right
`Host`/`Origin` headers, which is fiddly and easy to get subtly wrong. Running
Vite in the container keeps the proven same-origin proxy with one knob
(`VITE_PROXY_TARGET`) and no nginx. Honest cost: it's a dev server, not a
production build — fine for a demo; a real deploy would ship static assets via a
CDN with `CSRF_TRUSTED_ORIGINS` set to the real origins.

**What I'd do differently.** Three honest reflections. (1) `Idempotency-Key` is
*required* on credit issuance but only *honored-if-present* on line-item override
— overrides are value-idempotent so it's defensible, but a uniform money-moving
contract would require it on both. (2) The §4 scaling ceilings are reasoned, not
measured; with more time I'd add a seed-driven load-test harness and replace the
back-of-envelope numbers with real ones. (3) The lockless-ingest boundary is the
right call at this scale, but if an SLA ever demanded exact *per-invoice* (not
just per-aggregate) correctness, I'd revisit it with a shared advisory lock on
the ingest path and measure the hot-path cost rather than assume it.

## 7. What I didn't build, and would build next

Deliberately out of scope: **plan versioning** (late-event adjustments use the
current marginal rate, not the original period's — correct billing across rate
changes needs versioned plans); **per-endpoint pricing** (the `endpoint` field is
retained for it); **per-staff RBAC** (today any `is_staff` user is full-access;
Django Groups would split read-only vs admin); **MFA/SSO** for ops;
**multi-currency, DR, email/PDF invoicing, dunning, refund-reversal accounting**;
and the **observability stack itself** (metric/alert hooks are specified, the
dashboards aren't built). Each is sized small against the core, and none changes
the data model.
