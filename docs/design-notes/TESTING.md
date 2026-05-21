# Testing Strategy — Working Sketch

> The brief is unusually explicit: "Prioritize tests around correctness boundaries: idempotency, concurrency, tenant isolation, reconciliation behavior, and money-moving actions. We are not evaluating trivial coverage metrics." Backs the Code quality & testing rubric (8%) and is referenced from DESIGN.md §2.

## Anti-strategy

We don't test:
- Django ORM internals (their job)
- Trivial getters / `__str__` methods
- DRF serializer field plumbing
- That migrations apply (the framework guarantees this)
- 100% line coverage as a metric — coverage is incidental to value

## Strategy

Five buckets of high-value tests, organized by the brief's correctness boundaries.

### 1. Idempotency

| Test | What it proves |
|---|---|
| `test_event_ingest_dedup_within_batch` | Same `request_id` twice in one batch returns one `accepted` + one `duplicate` |
| `test_event_ingest_dedup_across_batches` | Replaying the same batch is a no-op |
| `test_event_ingest_concurrent_same_request_id` | 50 threads racing the same `request_id`: exactly 1 row inserted |
| `test_aggregator_idempotent_no_op` | Run aggregator twice with no new events → no row changes |
| `test_aggregator_idempotent_with_new_events` | Run, ingest, run again → window totals correctly updated, no double-counting |
| `test_invoicer_idempotent_for_period` | Calling `issue_monthly_invoice` twice for same customer-period returns same invoice, no extra rows |
| `test_invoicer_concurrent_for_same_customer` | 5 threads issuing the same invoice: 1 succeeds, 4 return the existing invoice |
| `test_webhook_replay_same_delivery_id` | Same delivery_id three times: 1 paid transition, audit log shows 1 row |
| `test_webhook_replay_different_delivery_same_payload` | New delivery_id, same `invoice_id`, already paid → no-op |
| `test_credit_issuance_double_click` | Same Idempotency-Key twice → 1 credit row, 1 audit row, both responses match |
| `test_credit_idempotency_key_with_different_body` | Same key, different amount → 422 conflict, no credit created |

### 2. Concurrency

| Test | What it proves |
|---|---|
| `test_aggregator_locks_out_invoicer_for_same_customer` | While invoicer holds the per-customer lock, aggregator blocks; after invoicer commits, aggregator sees sealed windows and skips them |
| `test_two_aggregators_singleton_lock` | Second aggregator instance returns early; no double-processing |
| `test_line_item_override_concurrent` | Two PATCH requests to same line item: both succeed in sequence; audit has 2 rows with correct before→middle→after chain |
| `test_seal_during_ingest_race` | Ingest an event for hour H while invoicer is sealing H's window: event lands; either `is_late=true` (correct) or aggregator-recovery flips it later (correct via different path); never silently uncounted |
| `test_select_for_update_skip_locked_invoicer_parallelism` | Two invoicer workers claiming customers via `SELECT … SKIP LOCKED`: each gets a distinct customer; total time ≈ half of single-worker |

Implementation note: concurrency tests use Django's `TransactionTestCase` (real txns), `threading`, and `pytest-django` `db` markers. Where Postgres advisory locks are involved, the test uses a real DB (not SQLite). Docker-compose includes a `test` Postgres on a separate port for CI.

### 3. Tenant isolation

| Test | What it proves |
|---|---|
| `test_customer_a_cannot_read_customer_b_invoice_by_id` | GET `/v1/invoices/{B_invoice_id}` as A → 404 (not 403) |
| `test_customer_a_cannot_read_customer_b_usage_by_api_key_param` | `/v1/usage?api_key_id={B_key_id}` as A → empty result, no error |
| `test_customer_scoped_manager_raises_without_scope` | `Event.objects.all()` raises `CustomerScopeMissing` |
| `test_cursor_token_from_other_tenant_returns_empty` | Forge a valid cursor pointing at customer B's event; query as A → empty |
| `test_grep_unsafe_all_tenants_only_in_allowlist` | Meta-test: greps the codebase for `.unsafe_all_tenants(` and asserts the call sites are exactly the documented allowlist (background jobs + ops viewsets) |
| `test_ops_endpoint_rejects_customer_api_key` | API key in `Authorization` header on `/ops/customers` → 403 |
| `test_customer_session_cannot_be_used_for_ops` | Customer session cookie on `/ops/customers` → 403 |

The last test is structural: ops authentication is a different backend (`SessionAuthentication` on `auth.User`), not a permission check after authentication.

### 4. Reconciliation behavior

| Test | What it proves |
|---|---|
| `test_late_event_creates_adjustment_line_item_next_invoice` | Event arrives after a window is sealed → next month's invoice has an adjustment line for those units |
| `test_late_event_marked_adjusted_after_processed` | Sweeper marks `adjusted_at`; same event isn't double-applied on subsequent runs |
| `test_drift_detector_window_vs_events` | Manually corrupt a window's `units_consumed`; drift job emits an alert and recomputes (if unsealed) |
| `test_drift_detector_sealed_window` | Drift on sealed window: alert fires, no auto-correction |
| `test_invoice_total_recompute_after_line_item_override` | Override a line item; invoice `total_micro_cents` updates atomically (verify pre/post in same txn read) |

### 5. Money-moving actions

| Test | What it proves |
|---|---|
| `test_tiered_pricing_math_zero_units` | 0 units → invoice total 0 |
| `test_tiered_pricing_math_at_tier_boundary` | Exactly 10,000 units → all in free tier; exactly 100,000 → free + tier 2; exactly 100,001 → all three tiers |
| `test_tiered_pricing_math_property_based` | Hypothesis: for any `units >= 0`, total = sum-of-tier-amounts; tier amounts are non-negative; ordering of tiers doesn't affect result |
| `test_credit_application_reduces_invoice_total` | Credit of $X applied to invoice → total reduced by X (with floor at 0) |
| `test_credit_floor_at_zero` | $100 invoice + $200 credit → total = $0, remaining $100 not visible (current design: credit is consumed in full as a line item; future: keep unused balance) |
| `test_audit_row_written_on_credit` | Credit issuance writes audit; audit row is read-only |
| `test_audit_immutability_via_trigger` | `UPDATE audit_log SET ...` raises; `DELETE FROM audit_log` raises |
| `test_audit_immutability_via_grants` | Connect as `app_role`; UPDATE/DELETE on audit_log denied at grant level |
| `test_money_in_micro_cents_not_floats` | Schema assertion: all money columns are `bigint`, no `numeric`/`float`/`real` |
| `test_invoice_total_matches_line_items_sum` | After issuance, `invoice.total = SUM(line_item.amount)` for that invoice |

## Property-based tests (Hypothesis)

Tiered pricing math is exactly the kind of thing Hypothesis catches that example-based tests miss.

```python
@given(
    units=st.integers(min_value=0, max_value=10**12),
    tiers=tier_strategy(),  # custom: non-overlapping, gap-free, ordered tiers
)
def test_tiered_pricing_invariants(units, tiers):
    plan = PricePlan.from_tiers(tiers)
    total = plan.compute_total(units)

    # Invariant 1: non-negative
    assert total >= 0
    # Invariant 2: sum of per-tier slice
    assert total == sum(
        plan.tier_amount(units, tier) for tier in tiers
    )
    # Invariant 3: monotonic in units
    if units > 0:
        assert plan.compute_total(units - 1) <= total
    # Invariant 4: doubling-trick — total(2u) >= 2*total(u) - first_tier_cost (handles free tier)
```

## Test pyramid

| Layer | Count est. | Tools |
|---|---|---|
| Unit (pricing math, money helpers, serializer validation) | ~30 | pytest, Hypothesis |
| Integration (full request → DB → response, w/ real Postgres) | ~40 | pytest-django, factory_boy |
| Concurrency (thread races, advisory locks) | ~8 | TransactionTestCase, threading |
| End-to-end (browser → API → DB) | 0–2 | Playwright (only if budget allows; brief de-emphasizes frontend polish) |

CI runs all unit + integration on every commit. Concurrency runs nightly + on PRs touching `aggregator/`, `invoicer/`, `webhooks/`.

## Test data

`factory_boy` factories for every model. A `RealisticGeneratorScript` (the seed script the brief requires) writes ~10 customers × 30 days of events at realistic rates (10–200/s per customer, varying), some early-month spikes, some events with `event_timestamp` 2 hours in the past (late-arrival simulation), some duplicates (replay simulation). Run with `python manage.py seed --customers=10 --days=30`.

## Coverage targets

Not a target. Coverage is **a side effect** of testing the boundaries above. Expected ranges:
- `apps/billing/aggregator.py` — high (this is the hot path)
- `apps/billing/invoicer.py` — high
- `apps/billing/webhook.py` — high
- `apps/billing/managers.py` — every method exercised
- `apps/api/views.py` — covered via integration tests
- `apps/api/serializers.py` — only the validation logic, not the field declarations
- Vendored / boilerplate — low; that's fine
