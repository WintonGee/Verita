# Operations & Scaling — Working Sketch

> Backs DESIGN.md §3 (aggregation pipeline, recomputable vs immutable), §4 (failure modes with concrete numbers), and the operational-thinking rubric line. The principle: a simpler system with strong correctness, where each scale ceiling is a known fix, not an architecture rewrite.

## Production target recap

- 5,000 active customers
- 200 events/sec sustained, 2,000/sec peak
- ~500M events/month (~17M/day, ~720k/hour)
- 5,000 × 24 × 30 = 3.6M usage_windows/month
- Up to 5,000 invoices/month
- Monthly invoices; accuracy is contractual

## What's recomputable vs immutable

| Data | Recomputable from | Immutability gate |
|---|---|---|
| `event` rows | — (source of truth) | append-only; no UPDATE/DELETE in app code (only `is_late`/`adjusted_at` flag transitions) |
| `usage_window.units_consumed` | `event` rows for that hour | `sealed_at IS NULL` (recomputable until invoice issuance) |
| `invoice.line_items` (usage kind) | `usage_window` × `price_plan` for the period | invoice status `issued` (rewrites require override flow with audit) |
| `invoice.total_micro_cents` | sum of own line items | recomputable while draft; once issued, the reconciliation job *detects* (does not auto-fix) drift — ops resolves via an audited credit/override |
| `audit_log` rows | — (source of truth for ops actions) | trigger-blocked UPDATE/DELETE; revoked grants |
| `webhook_delivery` rows | — (source of truth for inbound webhooks) | unique on `delivery_id` |

This is the "what does ops touch" map. If line-item totals disagree with windows, the windows are the ground truth (until sealed). If sealed window disagrees with events, the events are the ground truth — but you can't safely re-derive the invoice; you issue a credit instead.

## Drift reconciliation (the safety net)

Three daily jobs, all read-only, all alert-only:

```sql
-- Job 1: events sum vs window
SELECT
  customer_id, window_start,
  SUM(units_consumed) AS event_sum,
  uw.units_consumed AS window_sum,
  SUM(units_consumed) - uw.units_consumed AS delta
FROM event e
JOIN usage_window uw USING (customer_id)
WHERE e.event_timestamp >= now() - INTERVAL '7 days'
  AND date_trunc('hour', e.event_timestamp) = uw.window_start
GROUP BY customer_id, window_start, uw.units_consumed
HAVING SUM(units_consumed) <> uw.units_consumed;
```

If `delta <> 0` and `sealed_at IS NULL`: the window is still open, so the next
hourly aggregator run recomputes it; reconciliation only flags cases that persist.
If `delta <> 0` and `sealed_at IS NOT NULL`: alert; engineer-triggered credit
reconciliation. The reconciliation job itself is read-only — it detects, it does
not mutate. Threshold for alert vs page: any sealed-window drift pages immediately.

```sql
-- Job 2: line-item total vs invoice
SELECT i.id, i.total_micro_cents,
       (SELECT SUM(amount_micro_cents) FROM line_item WHERE invoice_id = i.id) AS line_sum
FROM invoice i
WHERE i.status IN ('issued', 'paid')
HAVING i.total_micro_cents <> line_sum;
```
Detected only (the job is read-only): a still-draft invoice is recomputed by
re-running issuance; an *issued* invoice is corrected via an audited
override/credit — never a silent UPDATE to a sealed total.

```sql
-- Job 3: stuck-state detector
SELECT id, customer_id, period_end, issued_at
FROM invoice
WHERE status = 'draft' AND period_end < now() - INTERVAL '2 days';
```
Alert: the invoicer cron didn't issue this one.

---

## Failure modes (the three that break first)

Each has a concrete trigger, a measured threshold, and a named fix. This is the section that distinguishes "won't scale" from "scales with a known fix" — the language the rubric uses.

### Failure mode 1 — Ingest write throughput
**Trigger**: sustained event rate > 3k/sec.
**Symptom**: `POST /v1/events` p99 latency climbs from ~30ms to ~250ms. B-tree contention on `event.UNIQUE(request_id)` shows up as `lock_waits` in `pg_stat_activity`.
**Why**: every insert traverses + locks an index page on a single B-tree. At 3k inserts/sec across random `request_id`s, page-level lock contention exceeds Postgres's MVCC fast path.
**Fix at this scale**: bump RDS instance class. Adds headroom but doesn't change architecture.
**Fix at 10× scale** (20k/sec sustained): write-aside buffer. Events land in Redis Stream or Kafka (200µs write); a flusher batches them into Postgres in chunks of 5,000 via `COPY` (10ms per chunk; ~50× throughput). Idempotency proof is unchanged because the buffer is also keyed by `request_id` (Redis SETNX or Kafka log compaction by key). Customer-facing response moves from 200 OK to 202 Accepted; SLA shifts from "ingested" to "received."
**Migration playbook**: dual-write to buffer + Postgres for a week; cut reader over; deprecate direct write.

### Failure mode 2 — Aggregator scan time
**Trigger**: event table exceeds ~1B rows (covering the latest 2 months at sustained 200/s).
**Symptom**: hourly aggregator run time exceeds 30 min; falls behind ingest. Dashboard latency for current-period usage climbs because windows are stale.
**Why**: full-hour scan over a 50GB B-tree on `(customer_id, event_timestamp)` walks ~720k rows, but the index is now interleaved across cold pages; cache misses dominate. Also: full-table VACUUM on the events table takes hours.
**Fix at this scale**: monthly range partitions on `event_timestamp` via `pg_partman`. Aggregator hits only the current partition (50M rows, ~5GB index). Drop partitions older than retention.
**Fix at 100× scale** (50B rows): shovel cold partitions to ClickHouse / BigQuery. Postgres keeps 3 hot months. Ingest path unchanged; analytics queries change.
**Migration playbook**: `pg_partman` install + convert in-place during a low-write window; new partitions auto-created hourly going forward.

### Failure mode 3 — Monthly invoicer wall clock
**Trigger**: customer count × per-customer invoice time exceeds the scheduled window. At 5k customers × 600ms = 50min.
**Symptom**: at 50k customers, single-worker wall time ≈ 8 hours. Overlaps with next-day aggregator runs; lock contention between invoicer and aggregator on the same customers.
**Why**: invoicer is single-threaded by design (one cron tick fires `issue_invoices` which loops over customers).
**Fix at this scale**: split into per-customer dispatch. Switch to a queue-based dispatcher (could be the locked-job-table pattern grown up, or introduce Celery at this point — Celery is the right answer when there are *many* tasks per second, not for once-a-month). Each worker `SELECT customer_id FROM customer WHERE last_invoiced_at < date_trunc('month', now()) FOR UPDATE SKIP LOCKED LIMIT 1`, processes, marks. N workers → N× speedup. Adding Celery here is one of the migrations called out — it gets earned at this scale.
**Fix at 100× scale**: same pattern, more workers, +partition the invoice table by month.
**Migration playbook**: add `last_invoiced_at` column; backfill from existing invoices; switch beat task from "loop" to "dispatch."

### Other things that grow but don't break first
- `audit_log`: ~10/day under normal load → bounded. At 1k staff or auto-audit on every event, partition by month.
- `webhook_delivery`: 5k invoices × 1 webhook/month = 5k rows/month. Negligible.
- `idempotency_key`: TTL of 24h means steady-state size ≪ 1M rows.

---

## Observability hooks

| Layer | Tool | Key signals |
|---|---|---|
| Metrics | Prometheus (statsd shim in Django) | event ingest rate, event dedup rate, aggregator lag (now − latest sealed window), invoicer success/failure counts, webhook valid/invalid rates, late-event backlog per customer |
| Logs | structlog JSON to stdout → CloudWatch / Loki | request `trace_id` on every line, customer_id where present, error.code |
| Traces | OpenTelemetry | span per HTTP request, per management-command run; ingest path traced end-to-end |
| Dashboards | Grafana | "Billing health" board: ingest rate, dedup %, aggregator lag, invoice issuance progress |
| DB | pg_stat_statements, pg_stat_user_indexes | top queries, index hit ratio |

## Specific alerts (what would page someone)

| Alert | Threshold | Severity | Why |
|---|---|---|---|
| `aggregator.lag` | > 2 hours | warn | windows are stale; dashboard data wrong |
| `aggregator.failed_runs` | ≥ 3 in a row | page | something's broken in the pipeline |
| `invoicer.failed_run` | any | page | financial cron failure |
| `drift.window` | > 0.5% of any sealed window | page | invariant violation; possible bug |
| `drift.unsealed_window` | > 5% | warn | likely transient |
| `webhook.invalid_signature_rate` | > 5%/hr sustained | page | possible attack or secret rotation gone wrong |
| `webhook.stale_timestamp_rate` | > 1%/hr | warn | clock skew or replay attempt |
| `event.ingest_5xx` | > 0.1%/min | page | data plane is breaking |
| `event.is_late_count` | > 100 per customer per day | warn | upstream client clock issue or replay |
| `idempotency_key.collision_with_diff_body` | any | warn | client bug worth surfacing |

## Debugging a wrong invoice — concrete playbook

The rubric line: "how ops debugs a wrong invoice." Walking through:

1. **Customer flags invoice `inv_abc`** (via support or self-service "dispute").
2. **Ops opens `/ops/customers/{id}` → `inv_abc`**, sees the line-item breakdown.
3. **Question 1: do line items sum to the invoice total?** The `invoice_total_drift` check (`run_reconciliation`) answers this. If they diverge on an *issued* invoice, correct it with an audited line-item override (`PATCH /ops/invoices/{id}/line-items/{id}`) or a credit — never a silent recompute of a sealed total.
4. **Question 2: do line-item units match `SUM(usage_window.units_consumed)` for the period?** Run `run_reconciliation` (the `window_drift` check compares window totals to the raw events). A mismatch → drift between windows and line items at issuance time. Engineer involved.
5. **Question 3: does `SUM(usage_window)` for the period match `SUM(event)`?** Reconciliation job catches this nightly; ops can also trigger on demand. If mismatch and windows are sealed → aggregator missed events.
6. **Question 4: are there `is_late` events for this period that didn't get adjusted?** Query `event WHERE customer_id=? AND event_timestamp BETWEEN period_start AND period_end AND is_late=true`. If any are unadjusted, they'll roll into next month; explain that to the customer.
7. **Resolution**:
   - **Genuine overcharge**: issue a credit via the ops console (`POST /ops/customers/{id}/credits`) with `reason="Invoice dispute resolved — inv_abc overcharge"`. Credit applies to next invoice.
   - **Genuine undercharge**: nothing to do; we eat it. Don't mutate a paid invoice without strong reason. If you must: line-item override (audited).
   - **Bug discovered**: file the bug, recompute the customer's invoice via the same credit path, write a post-mortem.

The audit log is what makes this debuggable months later. Any fix that touches money writes a row that captures actor, before, after, reason, ip.

---

## Scaling at 10× and 100× (table-by-table)

| Table | Today (1×) | 10× ceiling | 100× ceiling | What changes |
|---|---|---|---|---|
| `customer` | 5k rows | 50k | 500k | nothing; even at 500k, the table is tiny |
| `customer_user` | ~5k | 50k | 500k | nothing |
| `api_key` | ~15k | 150k | 1.5M | nothing |
| `event` | 500M/mo | 5B/mo | 50B/mo | partition by `event_timestamp` (10×); offload cold partitions to OLAP store (100×) |
| `usage_window` | 3.6M/mo | 36M/mo | 360M/mo | partition by `window_start`; same story |
| `invoice` | 5k/mo | 50k/mo | 500k/mo | partition by `period_start`; parallel invoicer (10×); per-region invoicers (100×) |
| `line_item` | ~25k/mo | 250k/mo | 2.5M/mo | follows invoice partition |
| `credit` | sparse | sparse | sparse | nothing |
| `audit_log` | sparse, but grows | partition monthly | cold-storage older partitions | nothing structural |
| `webhook_delivery` | 5k/mo | 50k/mo | 500k/mo | partition; bounded by invoice count |
| `idempotency_key` | bounded by TTL | bounded | bounded | nothing |

**No table needs sharding at 100× outside of `event` and `usage_window`** — those are the hot ones. Everything else is small enough to live on a single Postgres for years.

## Migration philosophy

The brief asks for "a clear evolutionary scaling path." Each fix above is a **shape-preserving** change:

- Buffer in front of ingest → API contract unchanged
- Partition `event` → all queries unchanged (partition-aware)
- Parallel invoicer → behavior unchanged, just dispatch differently
- ClickHouse cold storage → reads change for analytics, not for the billing path

None of them require rethinking the data model. That's the design goal: get the schema and the idempotency story right now, and every scaling step is "add infra," not "rewrite app."

## What's intentionally not built (will be DESIGN.md §7)

- Real-time billing (the seal model assumes monthly cadence)
- Multi-currency (currency column exists but only USD is handled)
- Plan versioning (today's plan applies to all historical periods on adjustment)
- Self-serve customer plan changes
- Customer-facing usage download (CSV/JSON export)
- Email invoice delivery (PDF, dunning)
- Refund handling beyond credits (no reversal accounting model)
- Multi-region deployment
- Field-level encryption in `audit_log.before/after` for sensitive payloads
- Per-staff RBAC (Django Groups: `billing-readonly` vs `billing-admin`)
- Customer-facing "dispute invoice" workflow with state machine
- MFA / SSO for ops staff
- API key scopes (today all keys have full read+write on the customer)
- Per-endpoint pricing
- Rate-limit visibility for customers (a `/v1/limits` endpoint)
