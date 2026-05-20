# Data Model — Working Sketch

> This is the design sketch. Once locked, Django migrations are a near-mechanical translation. Notes here will roll up into `DESIGN.md` §1 (Data model) and §2 (Idempotency).

## Conventions

- **Timestamps**: `timestamptz` everywhere. Server-side UTC. Never `timestamp` (naive).
- **Money**: `bigint amount_micro_cents`. 1 micro-cent = 1e-6 cent = $1e-8. Reason: pricing is $0.0005/unit; cents lose precision. micro-cents give 8 decimal places of USD precision. Bigint max (~9.2×10¹⁸) ÷ 1e8 ≈ **$92B per row** — plenty for any single invoice or aggregate. (If we ever needed $92T headroom, we'd switch to `numeric(28,8)`; flagged but unnecessary.)
- **IDs**: `uuid v4` primary keys on customer-visible entities (event, invoice, line_item, credit, api_key) so tenants cannot guess neighbors by incrementing. `bigserial` on internal tables (audit_log) where ordering matters and the IDs aren't exposed.
- **Soft delete**: nothing is hard-deleted except expired `idempotency_key` rows. Money records (events, invoices, line items, credits, audit) live forever.
- **Naming**: snake_case tables, singular nouns. `*_at` for timestamps. `*_micro_cents` for money. No `is_` prefix on bools — use `revoked_at IS NULL` etc.

## Entity overview

```
customer 1───* api_key
       │       
       │       
       └───* event ──┐
       │             │ (aggregated hourly)
       └───* usage_window  
       │             │ (rolled up monthly)
       └───* invoice 1───* line_item
       │
       └───* credit (applied to next invoice)
       │
       *───1 price_plan 1───* price_tier

audit_log         (append-only; all ops actions)
webhook_delivery  (replay dedupe)
idempotency_key   (mutation dedupe)
```

## Tables

### `customer` (the tenant org)
| col | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `name` | varchar(255) | |
| `billing_email` | varchar(255) | for ops contact / invoice delivery |
| `status` | enum: `active`/`suspended`/`closed` | suspended = blocked from ingestion |
| `price_plan_id` | uuid FK → price_plan(id), RESTRICT | current plan; plan changes go to next billing period (versioning is documented as future work) |
| `created_at` / `updated_at` | timestamptz | |

**Indexes**: `(status)` partial for `WHERE status='active'`.

**FK behavior**: Customer is never hard-deleted; `status = 'closed'` instead. RESTRICT on the plan FK protects against deleting a plan that has live customers.

---

### `customer_user` (browser login for the customer dashboard)
| col | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `customer_id` | uuid FK → customer, RESTRICT | which tenant this user belongs to |
| `email` | varchar(255) unique | login identity |
| `password_hash` | varchar(255) | argon2id (Django's `PASSWORD_HASHERS` default first entry) |
| `is_active` | bool | |
| `last_login_at` | timestamptz nullable | |
| `created_at` | timestamptz | |

**Indexes**: `(email)` unique, `(customer_id)`.

**Why a separate model from Django's `auth.User`**: ops staff use `auth.User` (Django admin + ops console). Customers use `customer_user`. Two distinct authentication backends means it's structurally impossible for a staff middleware to accidentally authenticate a customer user, or vice versa. Each backend sets a different attribute on `request` (`request.staff_user` vs `request.customer_user`), and tenant-scoped views require `request.customer_user`.

**Why per-tenant uniqueness isn't on email**: email is unique *globally* because we want one email = one identity. If a person has access to two customer tenants, they get two `customer_user` rows. (Cross-tenant access via a single user account is a future feature, not in scope.)

---

### `api_key`
| col | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `customer_id` | uuid FK → customer, RESTRICT | |
| `prefix` | varchar(12) unique | first chars of the key, for fast lookup. Format: `vk_live_xxxxxxxx` |
| `key_hash` | bytea | SHA-256 of (salt \|\| secret) |
| `salt` | bytea | per-key salt, 16 bytes |
| `name` | varchar(255) | human label ("production", "staging") |
| `last_used_at` | timestamptz nullable | bumped on successful auth (eventually consistent — see ops notes) |
| `revoked_at` | timestamptz nullable | once set, key auth fails |
| `created_at` | timestamptz | |

**Indexes**: `(prefix)` unique (hot path for auth lookup), `(customer_id, revoked_at)` for listing keys per customer.

**Why SHA-256 not bcrypt**: API keys are high-entropy random secrets (≥160 bits), not human passwords. A hash collision attack on SHA-256 + salt requires ≥2^80 work. bcrypt's slow-by-design property is only useful against guessable passwords. Hot path (every API request → auth lookup) needs to be µs, not ms. Will document as a trade-off.

**Key format shown once on creation**: `vk_live_a1b2c3d4_<32 random base62 chars>`. Customer copies it, we store hash + salt + prefix only.

---

### `price_plan`
| col | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `name` | varchar(255) | e.g. "Standard 2024" |
| `currency` | char(3) | only `USD` for take-home |
| `created_at` / `updated_at` | timestamptz | |

### `price_tier`
| col | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `price_plan_id` | uuid FK → price_plan, CASCADE | |
| `start_unit` | bigint | inclusive lower bound (0, 10000, 100000) |
| `end_unit` | bigint nullable | exclusive upper bound; NULL = infinity |
| `unit_price_micro_cents` | bigint | 0 for free tier |
| `ordinality` | smallint | for stable ordering |

**Constraints**:
- `UNIQUE(price_plan_id, start_unit)`
- `CHECK (end_unit IS NULL OR end_unit > start_unit)`
- `CHECK (unit_price_micro_cents >= 0)`
- Application-level invariant: tiers cover [0, ∞) without gaps. Validated on plan creation.

---

### `event`
| col | type | notes |
|---|---|---|
| `id` | bigserial pk | internal only; not exposed in API |
| `customer_id` | uuid FK → customer, RESTRICT | |
| `api_key_id` | uuid FK → api_key, RESTRICT | which key was used |
| `request_id` | varchar(64) | globally unique per brief |
| `endpoint` | varchar(255) | for future per-endpoint pricing / analytics |
| `units_consumed` | int | `CHECK >= 0` |
| `event_timestamp` | timestamptz | when the API call happened (provided by client) |
| `ingested_at` | timestamptz default now() | when we received it |

**Indexes**:
- `UNIQUE(request_id)` — the idempotency guarantee
- `(customer_id, event_timestamp DESC)` — serves `GET /v1/usage` and the aggregator's per-customer slice
- `(event_timestamp)` BRIN — for aggregator full-hour scans; BRIN because the table is append-only by timestamp (perfect BRIN fit, ~1000× smaller than B-tree)
- `(api_key_id, event_timestamp DESC)` — `GET /v1/usage?api_key=...`

**Why bigserial PK not uuid**: Internal ID, never exposed. bigserial is 8 bytes vs uuid's 16, halves the PK index size on a 500M-row table. We pay a small monotonic-insert penalty but the table is append-only at the tail anyway.

**Why CHECK on units_consumed**: hostile customer can't send negative units to inflate other customers' bills (they can only send to their own) — but malformed clients can. Constraint blocks at write.

**At 10× / 100×**:
- 10× = 5B rows/year. Single-table B-tree on (customer_id, event_timestamp) is ~150GB. Still queryable but vacuum/maintenance windows grow.
- **Fix**: Range-partition by `event_timestamp` monthly. Drop partitions older than retention period. Aggregator only touches the current partition.
- 100× = 50B rows/year. Postgres can technically do this but operationally painful. **Migration**: shovel cold partitions to ClickHouse/BigQuery; keep ~3 hot months in Postgres for aggregation. Ingestion path stays the same — only analytics changes.

---

### `usage_window`
| col | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `customer_id` | uuid FK → customer, RESTRICT | |
| `window_start` | timestamptz | `date_trunc('hour', ...)` |
| `units_consumed` | bigint | sum across all events in window |
| `event_count` | int | for debugging/reconciliation |
| `sealed_at` | timestamptz nullable | when aggregator stopped re-computing this window |
| `last_recomputed_at` | timestamptz | for drift detection |

**Indexes**:
- `UNIQUE(customer_id, window_start)` — both correctness (one row per slot) and the UPSERT target
- `(window_start) WHERE sealed_at IS NULL` partial — aggregator's "what's still recomputable" scan
- `(customer_id, window_start DESC)` — dashboard chart query

**Sealing semantics**: A window is **recomputable until its parent invoice is issued**. Aggregator UPSERTs idempotently. After invoice issuance, `sealed_at` is set on every window in the period; late events for that period flow to a separate "corrections" path (see Pipeline doc — TBD).

**Why store `units_consumed` even though it's derivable**: read perf for `/v1/usage` charts. Recomputable from `event` rows in a reconciliation job (drift check).

---

### `invoice`
| col | type | notes |
|---|---|---|
| `id` | uuid pk | exposed in URLs |
| `customer_id` | uuid FK → customer, RESTRICT | |
| `period_start` | timestamptz | inclusive |
| `period_end` | timestamptz | exclusive — first second of next month |
| `status` | enum: `draft`/`issued`/`paid`/`void` | |
| `total_micro_cents` | bigint | denormalized sum of line_items; CHECK >= 0 |
| `currency` | char(3) | snapshot from plan at issuance |
| `issued_at` | timestamptz nullable | |
| `paid_at` | timestamptz nullable | |
| `payment_delivery_id` | varchar(128) nullable | from webhook |

**Indexes**:
- `UNIQUE(customer_id, period_start)` — one invoice per customer per month
- `(status, period_end)` — issuer cron scans `WHERE status='draft' AND period_end <= now()`
- `(customer_id, period_start DESC)` — customer's invoice list

**State machine**: `draft → issued → paid` (happy path); `draft → void` (we never issued); `issued → void` (rare; needs audit). No transition into `draft` from `issued`.

**Why a denormalized total**: customer dashboard and ops list both render totals constantly. Recompute is one SUM but we'd hit it on every page render. Worth the denorm with a check via reconciliation job.

---

### `line_item`
| col | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `invoice_id` | uuid FK → invoice, RESTRICT | |
| `description` | varchar(500) | e.g. "Tier 2: 90,000 units @ $0.001" |
| `units` | bigint | |
| `unit_price_micro_cents` | bigint | snapshotted from tier at issuance |
| `amount_micro_cents` | bigint | `CHECK >= 0` for non-credit lines; credits use separate model |
| `tier_ordinal` | smallint nullable | which tier; null for adjustment lines |
| `kind` | enum: `usage`/`credit_application`/`adjustment` | |
| `overridden_at` | timestamptz nullable | |
| `override_reason` | text nullable | |
| `created_at` | timestamptz | |

**Indexes**: `(invoice_id)` — only access pattern.

**Override semantics**: PATCH writes new amount + audit row in the same transaction. Original amount is recoverable from the audit log (before/after JSON). No "previous_amount" column on the line item itself — audit log is the source of truth for history.

---

### `credit`
| col | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `customer_id` | uuid FK → customer, RESTRICT | |
| `amount_micro_cents` | bigint | `CHECK > 0` |
| `reason` | text | required |
| `issued_by_staff_id` | varchar(255) | staff email or user ID |
| `idempotency_key` | varchar(255) | matches header from ops UI |
| `applied_to_invoice_id` | uuid FK → invoice nullable | null until consumed |
| `created_at` | timestamptz | |

**Indexes**:
- `UNIQUE(customer_id, idempotency_key)` — double-click protection
- `(customer_id) WHERE applied_to_invoice_id IS NULL` — invoice generator finds pending credits

**Application model**: Credits are **applied at next invoice generation** as a `kind='credit_application'` line item with negative `amount_micro_cents`. The credit row's `applied_to_invoice_id` is set in the same transaction. This keeps every invoice self-explanatory (sum of its line items = total).

---

### `audit_log` (append-only)
| col | type | notes |
|---|---|---|
| `id` | bigserial pk | |
| `created_at` | timestamptz default now() | |
| `actor_type` | enum: `staff`/`system`/`customer` | |
| `actor_id` | varchar(255) | staff email, system component, or customer id |
| `action` | varchar(64) | e.g. `credit.issue`, `line_item.override`, `invoice.issue` |
| `resource_type` | varchar(64) | `invoice`, `credit`, `line_item` |
| `resource_id` | varchar(64) | |
| `before` | jsonb nullable | |
| `after` | jsonb | |
| `reason` | text nullable | required for credit/override |
| `request_ip` | inet nullable | |

**Indexes**:
- `(resource_type, resource_id, created_at DESC)` — "show me the history of this invoice"
- `(created_at DESC)` — global audit feed for ops

**Immutability**:
1. Postgres trigger: `BEFORE UPDATE OR DELETE ON audit_log RAISE EXCEPTION`.
2. App-role grants: `GRANT INSERT, SELECT ON audit_log TO app_role; REVOKE UPDATE, DELETE`.
3. Migrations run as a separate `migrator_role` that has full DDL but the application connection cannot.

Belt-and-suspenders: even a SQL-injection attacker holding the `app_role` cannot mutate audit rows.

---

### `webhook_delivery`
| col | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `delivery_id` | varchar(128) | from the payment provider's header |
| `received_at` | timestamptz default now() | |
| `signature_valid` | boolean | |
| `payload_sha256` | bytea | for forensic verification |
| `payload` | jsonb | raw body |
| `processed_at` | timestamptz nullable | |
| `result` | enum: `accepted`/`rejected_signature`/`rejected_stale`/`duplicate`/`error` | |
| `error_message` | text nullable | |

**Indexes**: `UNIQUE(delivery_id)` — the replay-dedup guarantee.

---

### `idempotency_key`
| col | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `customer_id` | uuid FK nullable | set for `/v1` actions |
| `staff_id` | varchar(255) nullable | set for `/ops` actions |
| `key` | varchar(255) | from `Idempotency-Key` header |
| `method` | varchar(8) | |
| `path` | varchar(500) | |
| `request_hash` | bytea | sha256 of canonical body — mismatch returns 422 |
| `response_status` | smallint | |
| `response_body` | jsonb | |
| `created_at` | timestamptz default now() | |
| `expires_at` | timestamptz | created_at + 24h |

**Indexes**:
- `UNIQUE(customer_id, key)` partial WHERE customer_id IS NOT NULL
- `UNIQUE(staff_id, key)` partial WHERE staff_id IS NOT NULL
- `(expires_at)` for the cleanup job

**Why scoped to actor**: prevents one customer's idempotency key from colliding with another's, and prevents key enumeration across tenants.

---

## Query → index trace (proving every index earns its place)

| Query | Where it runs | Index used |
|---|---|---|
| Insert event with dedup | `POST /v1/events` | `event.UNIQUE(request_id)` |
| List events for customer + date range | `GET /v1/usage` | `event(customer_id, event_timestamp DESC)` |
| Aggregator scans hour | hourly cron task | `event(event_timestamp)` BRIN + `(customer_id, event_timestamp)` for group |
| List customer invoices | `GET /v1/invoices` | `invoice(customer_id, period_start DESC)` |
| Get one invoice (scoped) | `GET /v1/invoices/{id}` | PK + tenant check on `customer_id` |
| Auth: lookup key by prefix | every API request | `api_key(prefix)` unique |
| Issue invoice (find draft due) | monthly cron task | `invoice(status, period_end)` |
| Find pending credits | invoice generator | `credit(customer_id) WHERE applied_to_invoice_id IS NULL` |
| Audit feed for resource | ops detail view | `audit_log(resource_type, resource_id, created_at DESC)` |
| Replay dedup | `POST /webhooks/payments` | `webhook_delivery.UNIQUE(delivery_id)` |
| Anomaly: customer's 30d avg | ops detail view | `usage_window(customer_id, window_start DESC)` |

Every column has a query backing it. Every index serves a named call site.

---

## Locked decisions (resolved forks)

| Fork | Decision | Schema impact |
|---|---|---|
| Customer browser auth | **Separate session login** (email+password → httpOnly cookie) | Added `customer_user` table. Argon2id hashing. Staff use Django's `auth.User`, customers use `customer_user` — two distinct auth backends |
| Pricing granularity | **Aggregate units across all endpoints**, single tiered plan per customer | `event.endpoint` stays for analytics only. One `price_plan` per customer via `customer.price_plan_id`. Per-endpoint pricing is documented as future work |
| Late-event handling | **Sealed at invoice issuance; late events become adjustments on next invoice** | `usage_window.sealed_at` set in same txn as invoice issuance. New `is_late` bool on `event` (default false), set TRUE if its window is already sealed at ingestion time. Sweeper rolls up `is_late=TRUE AND adjusted_at IS NULL` events into a `kind='adjustment'` line item on the next draft invoice. Indexed `(customer_id) WHERE is_late = TRUE AND adjusted_at IS NULL` |

The `event` table grows two columns to support this:

| col | type | notes |
|---|---|---|
| `is_late` | bool default false | TRUE if `usage_window(customer_id, window_start).sealed_at IS NOT NULL` at insert time |
| `adjusted_at` | timestamptz nullable | set when this event has been rolled into an adjustment line item |

Updated `event` indexes:
- `UNIQUE(request_id)` — idempotency
- `(customer_id, event_timestamp DESC)` — `/v1/usage` + aggregator per-customer
- `(event_timestamp)` BRIN — aggregator full-hour scan
- `(api_key_id, event_timestamp DESC)` — `/v1/usage?api_key=...`
- `(customer_id) WHERE is_late = TRUE AND adjusted_at IS NULL` partial — late-event sweeper

## Reconciliation jobs (recomputable safety net)

These are not in the request/response path; they run as low-priority background tasks and exist to detect drift between the denormalized totals and the source of truth.

| Job | Detects | Action |
|---|---|---|
| `events_vs_windows_drift` | `usage_window.units_consumed` ≠ `SUM(event.units_consumed)` for that hour | Alert + automatic re-aggregate if delta < threshold; require human if larger |
| `windows_vs_invoice_drift` | sum of issued invoice's line items ≠ `SUM(usage_window.units_consumed * tier_price)` for the period | Alert ops; never silently re-issue an issued invoice |
| `invoice_total_drift` | `invoice.total_micro_cents` ≠ `SUM(line_item.amount_micro_cents)` | Alert; recompute the denormalized total |
| `idempotency_key_cleanup` | rows where `expires_at < now()` | Delete (the only table where hard-delete happens) |

Next: `PIPELINE.md` — the three flows (ingest, aggregate, invoice) plus the webhook handler, with locking strategy and concrete pseudocode for each idempotency claim.
