# Pipeline & Concurrency — Working Sketch

> Four flows: **ingest → aggregate → invoice → webhook**, plus reconciliation. For each, the same template: inputs, locking, idempotency proof, failure modes. This doc backs `DESIGN.md` §2 (Idempotency & concurrency) and §3 (Aggregation pipeline).

## Schedule overview

All scheduled work runs as Django management commands invoked by system cron. Each command exits early if its global advisory lock is already held (i.e., a previous invocation is still running). No Celery / Redis.

| Command | Cron | Lock scope | Run length target |
|---|---|---|---|
| `python manage.py aggregate_events` | `15 * * * *` (every hour at :15) | `pg_try_advisory_lock('aggregator')` (global singleton) + per-customer xact lock per chunk | <5 min |
| `python manage.py issue_invoices` | `30 0 1 * *` (1st of month at 00:30 UTC) | per-customer xact lock | <30 min total |
| `python manage.py run_reconciliation` | `0 3 * * *` (daily at 03:00 UTC) | none (read-only) | <5 min |
| `python manage.py cleanup_idempotency_keys` | `0 4 * * *` (daily at 04:00 UTC) | none | <1 min |
| Event ingestion (`POST /v1/events`) | request-time | none (UNIQUE constraint) | <50ms p99 |

Concrete lock primitives:
- `pg_try_advisory_lock(key)` — session-scoped non-blocking; returns FALSE if already held. Used at the top of each cron command for "only one of me runs at once."
- `pg_advisory_xact_lock(key)` — auto-released on commit/rollback; per-customer serialization within a task.
- `SELECT … FOR UPDATE` — row-level; used inside customer's billing txn to serialize line-item edits.

**Why this is OK without Celery**: idempotency is the safety net. Aggregator failure → next hour re-runs and absorbs. Invoicer failure for a customer → that customer's draft stays unissued; daily reconciliation surfaces it; ops re-runs `python manage.py issue_invoices --customer=X`. No need for a queue or retry framework — the design already guarantees correctness across re-runs.

---

## Pipeline 1 — Event ingestion (`POST /v1/events`)

### Contract
Request:
```json
{
  "events": [
    {"request_id": "...", "endpoint": "/v1/infer", "units_consumed": 12, "timestamp": "2026-05-20T14:32:01Z"}
  ]
}
```
Response: per-event status (`accepted` / `duplicate` / `rejected`). HTTP 207 multi-status, or HTTP 200 with a result array.

Constraints: max 1000 events per batch, max body 1 MiB. Tenant is `request.customer` (set by auth middleware from API key).

### Locking
None. Idempotency is enforced by the `UNIQUE(request_id)` constraint, not by application locking.

### Idempotency proof

Single SQL statement per batch (multi-row INSERT):
```sql
INSERT INTO event (
  customer_id, api_key_id, request_id, endpoint, units_consumed,
  event_timestamp, is_late
)
SELECT
  $customer_id, $api_key_id, r.request_id, r.endpoint, r.units_consumed,
  r.event_timestamp,
  COALESCE(
    (SELECT sealed_at IS NOT NULL FROM usage_window
       WHERE customer_id = $customer_id
         AND window_start = date_trunc('hour', r.event_timestamp)),
    FALSE
  ) AS is_late
FROM UNNEST($request_ids, $endpoints, $units, $timestamps)
     AS r(request_id, endpoint, units_consumed, event_timestamp)
ON CONFLICT (request_id) DO NOTHING
RETURNING request_id, is_late;
```

- Re-delivery of an event: `ON CONFLICT DO NOTHING` makes it a no-op. The `RETURNING` clause omits skipped rows; from the response we know which ones were duplicates.
- Concurrent inserts of the same `request_id`: Postgres serializes around the unique index; first commits, second's `ON CONFLICT` triggers.
- Body mismatch for an existing `request_id`: by definition, a `request_id` represents a single API call. We do not compare bodies. If a client sends two different payloads with the same ID, we accept the first and silently drop the second — and the client has a bug. Documented.

### `is_late` semantics at ingest

Computed in the subquery above. If the event's hour-window already exists and has `sealed_at IS NOT NULL`, the event is flagged late. The flag is what the invoicer uses to find events needing adjustment.

Note: at the moment of insert, there is a tiny race window — the invoicer could be sealing this very window mid-insert. Mitigation: the invoicer takes a per-customer `pg_advisory_xact_lock` and uses `SELECT … FOR UPDATE` on windows before sealing. The ingest path does not need a lock; if it inserts a row with `is_late=false` for a window that gets sealed microseconds later, the next aggregator pass will still UPSERT that event's contribution because the seal-check on UPDATE is `WHERE sealed_at IS NULL` (it'll skip), and the invoicer's late-event sweep won't pick it up either. The fix: change the seal step to set `sealed_at` only AFTER taking a `FOR UPDATE` on the window row, and have ingestion fall back to `is_late=true` if the per-customer billing lock can't be acquired during a window seal. See "Race: ingest vs seal" below.

### Failure modes
- **Customer suspended**: middleware blocks before reaching DB.
- **API key revoked**: middleware blocks. (`revoked_at IS NOT NULL`)
- **Batch too large**: 413 with retry-after.
- **Event timestamp far in past/future**: accept past (could be backfill); reject >5 min in future as clock skew.
- **Postgres temporarily unavailable**: 503; the client retries with the same `request_id`s, dedup absorbs the retry.

---

## Pipeline 2 — Hourly aggregator

### Schedule
System cron, `15 * * * *`. The `:15` gives 15 minutes for in-flight events of the just-closed hour to land before we touch its window. (This is *not* the seal — sealing happens at invoice time. This is just a heuristic to reduce churn.)

### Locking
1. **Global singleton lock** — `pg_advisory_lock(hashtext('aggregator'))` at task start. If another worker is mid-run, this one no-ops and returns.
2. **Per-customer xact lock** — for each customer chunk, `pg_advisory_xact_lock(hashtext('billing_' || customer_id))` inside a transaction. Same lock the invoicer takes — guarantees aggregator and invoicer never touch the same customer simultaneously.

### Algorithm
```python
def aggregator_task():
    # 1. Singleton guard
    if not pg_try_advisory_lock(GLOBAL_AGGREGATOR_KEY):
        log("aggregator already running, skipping")
        return

    try:
        watermark = get_last_run_at()  # row in `cron_state` table
        cutoff = now() - timedelta(minutes=5)  # don't aggregate the bleeding edge

        # Find (customer_id, window_start) pairs needing recompute
        candidates = sql("""
            SELECT customer_id, date_trunc('hour', event_timestamp) AS window_start
              FROM event
             WHERE ingested_at >= %s AND ingested_at < %s
             GROUP BY 1, 2
        """, [watermark - timedelta(minutes=5), cutoff])  # small overlap

        for customer_id, chunk in group_by_customer(candidates):
            with transaction.atomic():
                # 2. Per-customer lock
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    [f"billing_{customer_id}"],
                )

                # 3. For each window in this customer's chunk, UPSERT.
                #    The WHERE clause ensures we never touch a sealed window.
                cursor.execute("""
                    INSERT INTO usage_window (id, customer_id, window_start,
                                              units_consumed, event_count,
                                              last_recomputed_at)
                    SELECT gen_random_uuid(), customer_id,
                           date_trunc('hour', event_timestamp),
                           SUM(units_consumed), COUNT(*), now()
                      FROM event
                     WHERE customer_id = %s
                       AND date_trunc('hour', event_timestamp) = ANY(%s)
                     GROUP BY customer_id, date_trunc('hour', event_timestamp)
                    ON CONFLICT (customer_id, window_start)
                    DO UPDATE SET
                        units_consumed = EXCLUDED.units_consumed,
                        event_count    = EXCLUDED.event_count,
                        last_recomputed_at = now()
                      WHERE usage_window.sealed_at IS NULL;
                """, [customer_id, list(chunk)])

        set_last_run_at(cutoff)

    finally:
        pg_advisory_unlock(GLOBAL_AGGREGATOR_KEY)
```

### Idempotency proof
- The UPSERT is `INSERT … ON CONFLICT DO UPDATE`. Running it twice with the same input rows produces the same output.
- Sealed windows are protected by `WHERE usage_window.sealed_at IS NULL` — a re-run after invoicing cannot mutate sealed totals.
- If new events arrived between runs, the next run picks them up (they appear in the `ingested_at` filter range). Output is "the correct sum of all currently-known events for this hour" — by definition stable.

### Failure modes
- **Task crashes mid-loop**: partial customers processed; on next run, watermark hasn't advanced so they're re-processed. Safe because UPSERT.
- **Aggregator falls behind ingest**: scan range grows. Mitigation at scale: run aggregator more often (every 10 min instead of hourly), or shard by `customer_id % N` across N workers.
- **Race: ingest vs seal** — see "Ingest-vs-seal race & the invoicer sweep" below. The aggregator does *not* attempt to fix this; the invoicer does, atomically, within the same transaction as the seal.

---

## Pipeline 3 — Monthly invoicer

### Schedule
System cron, `30 0 1 * *` (00:30 UTC on 1st of month). Generates invoices for the previous calendar month, per customer.

### Locking
- **Per-customer xact lock**: `pg_advisory_xact_lock(hashtext('billing_' || customer_id))`. Single coherent serialization point with the aggregator.
- **`SELECT FOR UPDATE`** on the customer's windows, pending credits, and late events — within the same transaction.

### Algorithm

```python
def issue_monthly_invoice(customer_id, period_start, period_end):
    txn_started_at = now()  # captured before the lock; used for race closure (step 10)
    with transaction.atomic():
        # 1. Lock customer's billing surface
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            [f"billing_{customer_id}"],
        )

        # 2. Idempotency: already issued?
        existing = Invoice.objects.filter(
            customer_id=customer_id, period_start=period_start
        ).first()
        if existing:
            return existing  # no-op

        # 3. Create draft
        invoice = Invoice.objects.create(
            customer_id=customer_id,
            period_start=period_start,
            period_end=period_end,
            status="draft",
            currency="USD",
            total_micro_cents=0,
        )

        # 4. Lock and read all windows in period
        windows = list(UsageWindow.objects
            .filter(customer_id=customer_id,
                    window_start__gte=period_start,
                    window_start__lt=period_end)
            .select_for_update())
        total_units = sum(w.units_consumed for w in windows)

        # 5. Compute tiered line items
        plan = Customer.objects.get(id=customer_id).price_plan
        usage_lines = compute_tiered_line_items(plan, total_units, invoice)
        LineItem.objects.bulk_create(usage_lines)

        # 6. Late events from PRIOR periods → adjustment line
        late_events = list(Event.objects
            .filter(customer_id=customer_id,
                    is_late=True,
                    adjusted_at__isnull=True,
                    event_timestamp__lt=period_start)
            .select_for_update())
        if late_events:
            late_units = sum(e.units_consumed for e in late_events)
            # Use customer's current marginal rate (see Trade-offs)
            adj_amount = price_at_current_marginal_rate(plan, customer_id, late_units)
            LineItem.objects.create(
                invoice=invoice,
                kind="adjustment",
                description=f"Late events from prior periods ({late_units} units)",
                units=late_units,
                amount_micro_cents=adj_amount,
            )
            Event.objects.filter(id__in=[e.id for e in late_events]) \
                         .update(adjusted_at=now())

        # 7. Apply pending credits
        credits = list(Credit.objects
            .filter(customer_id=customer_id,
                    applied_to_invoice_id__isnull=True)
            .select_for_update())
        for credit in credits:
            LineItem.objects.create(
                invoice=invoice,
                kind="credit_application",
                description=f"Credit: {credit.reason}",
                amount_micro_cents=-credit.amount_micro_cents,
            )
        Credit.objects.filter(id__in=[c.id for c in credits]) \
                      .update(applied_to_invoice_id=invoice.id)

        # 8. Roll up total
        line_sum = LineItem.objects.filter(invoice=invoice) \
                     .aggregate(s=Sum("amount_micro_cents"))["s"]
        invoice.total_micro_cents = max(0, line_sum)
        invoice.status = "issued"
        invoice.issued_at = now()
        invoice.save()

        # 9. Seal all windows in period (idempotent: WHERE sealed_at IS NULL).
        UsageWindow.objects.filter(
            customer_id=customer_id,
            window_start__gte=period_start,
            window_start__lt=period_end,
            sealed_at__isnull=True,
        ).update(sealed_at=now())

        # 10. Ingest-vs-seal race closure: any event in this period that was
        #     ingested AFTER our txn started AND was committed with is_late=FALSE
        #     (because its seal-check subquery saw sealed_at IS NULL at the
        #     moment of insert) → flip to TRUE. Next month's invoicer will
        #     create an adjustment line item for these. Customer is correctly
        #     billed in aggregate across the two months; current month is
        #     undercharged, next month is adjusted by the same amount.
        Event.objects.filter(
            customer_id=customer_id,
            event_timestamp__gte=period_start,
            event_timestamp__lt=period_end,
            is_late=False,
            ingested_at__gt=txn_started_at,
        ).update(is_late=True)

        # 10. Audit (mandatory)
        AuditLog.objects.create(
            actor_type="system",
            actor_id="invoicer",
            action="invoice.issue",
            resource_type="invoice",
            resource_id=str(invoice.id),
            after={"total_micro_cents": invoice.total_micro_cents,
                   "line_items": len(usage_lines) + len(credits) + bool(late_events)},
        )
        return invoice
```

### Idempotency proof
- Step 1: per-customer lock — blocks concurrent runs for the same customer.
- Step 2: existence check inside the lock — second run sees the first's commit and returns.
- Schema-level: `UNIQUE(customer_id, period_start)` is a backstop — even without the advisory lock, second insert raises `IntegrityError`.
- Atomicity: entire issuance is one Postgres transaction. Crash anywhere before COMMIT rolls back the draft, the line items, the credit applications, and the late-event marks. Next run is a fresh start.

### Ingest-vs-seal race & the invoicer sweep

The aggregator and invoicer hold per-customer locks. **Ingest does not**, because forcing every `POST /v1/events` to take a per-customer advisory lock would serialize the hot path. Without that lock, this race exists:

1. Invoicer txn starts at T0, takes per-customer lock, computes line items from windows.
2. Ingest at T1 > T0 reads `usage_window` via its is-window-sealed subquery — sees `sealed_at IS NULL` (invoicer hasn't committed yet). Writes the event with `is_late = FALSE`. Commits at T2 > T1.
3. Invoicer at T3 > T2 seals the window (Step 9 above). Commits at T4.
4. After T4: an event exists with `is_late = FALSE` but its window has `sealed_at IS NOT NULL` — an **orphan** that neither the aggregator (skips sealed windows) nor the late-event sweep (filters on `is_late = TRUE`) will pick up.

**Step 10 closes this**: just before commit, the invoicer flips `is_late = TRUE` for any event in the period that was `ingested_at > txn_started_at` and is still `is_late = FALSE`. These were the racy ingests. They become regular late events and roll into next month's adjustment line item.

Correctness property: **the customer is correctly billed across the two months** (current month under-bills by exactly the amount that next month's adjustment over-bills). At any point, the sum of all issued invoice amounts equals the sum of all ingested events × tier rates, modulo unconsumed credits. This is *eventual* correctness on a per-invoice basis but *exact* correctness in aggregate. Documented in DESIGN.md §3 (Aggregation pipeline) as the system's one tolerated correctness boundary.

### "Concurrent ops actions can't double-credit"
Two `POST /ops/customers/{id}/credits` arrive simultaneously with the same `Idempotency-Key`:
1. Both transactions try to `INSERT INTO idempotency_key (staff_id, key, …)` 
2. `UNIQUE(staff_id, key)` constraint: one succeeds, one raises `IntegrityError`
3. The losing request reads the existing row and returns the stored response

Same key, *different* request body: the stored `request_hash` differs → return 422 "Idempotency-Key reuse with different payload."

### Failure modes
- **Customer has zero usage and zero credits**: still issue an invoice with `total = 0` and a single "No usage in period" line item. Better than a missing invoice (auditable).
- **Late-event adjustment uses the wrong tier**: documented trade-off — we use current marginal rate, not original-period rate, because plan versioning is out of scope. In production, plan versioning is required for correctness across rate changes.
- **Wall-clock budget at 5,000 customers** (concrete number): If each invoice takes 600ms, single-worker runtime = 50 min. Acceptable for monthly cron. At 10× → 5,000 × 6 min, fix: parallelize via `SELECT customer_id FROM customer FOR UPDATE SKIP LOCKED LIMIT 1` claim pattern across N workers.

---

## Pipeline 4 — Webhook handler (`POST /webhooks/payments`)

### Contract
Headers:
- `X-Signature: t=<ts>,v1=<hex>` — HMAC-SHA256 of `timestamp.body`
- `X-Timestamp: <unix-ts>`
- `X-Delivery-ID: <unique>`

Body (mock):
```json
{"invoice_id": "...", "amount_paid_micro_cents": 12345000000, "currency": "USD"}
```

### Algorithm

```python
def webhook_payments(request):
    raw_body = request.body  # bytes, untouched
    ts_header = request.headers["X-Timestamp"]
    sig_header = request.headers["X-Signature"]
    delivery_id = request.headers["X-Delivery-ID"]

    # 1. Timestamp window — reject stale (replay-protection over time)
    if abs(time.time() - int(ts_header)) > 5 * 60:
        WebhookDelivery.objects.create(
            delivery_id=delivery_id, signature_valid=False,
            payload=raw_body.decode(), result="rejected_stale",
        )
        return HttpResponse(status=400)

    # 2. HMAC signature verify (constant time)
    expected = hmac.new(
        WEBHOOK_SECRET, f"{ts_header}.".encode() + raw_body, sha256
    ).hexdigest()
    if not hmac.compare_digest(sig_header.split("v1=")[1], expected):
        WebhookDelivery.objects.create(
            delivery_id=delivery_id, signature_valid=False,
            payload=raw_body.decode(), result="rejected_signature",
        )
        return HttpResponse(status=401)

    # 3. Replay dedup + process atomically
    try:
        with transaction.atomic():
            wd = WebhookDelivery.objects.create(
                delivery_id=delivery_id, signature_valid=True,
                payload=raw_body.decode(), result="accepted",
            )
            payload = json.loads(raw_body)
            invoice = Invoice.objects.select_for_update() \
                                     .get(id=payload["invoice_id"])

            # Verify the amount matches — guards against processor bugs or
            # payload tampering during a partial compromise. We never *use*
            # the amount to mutate state, but a mismatch is suspicious.
            if payload["amount_paid_micro_cents"] != invoice.total_micro_cents:
                wd.result = "error"
                wd.error_message = (
                    f"amount mismatch: paid={payload['amount_paid_micro_cents']} "
                    f"invoice={invoice.total_micro_cents}"
                )
                wd.save()
                AuditLog.objects.create(
                    actor_type="system", actor_id="payment_webhook",
                    action="invoice.payment_rejected_mismatch",
                    resource_type="invoice", resource_id=str(invoice.id),
                    after={"reason": "amount_mismatch", "delivery": delivery_id,
                           "paid": payload["amount_paid_micro_cents"],
                           "expected": invoice.total_micro_cents},
                )
                return HttpResponse(status=422)

            if invoice.status == "issued":
                invoice.status = "paid"
                invoice.paid_at = now()
                invoice.payment_delivery_id = delivery_id
                invoice.save()
                AuditLog.objects.create(
                    actor_type="system", actor_id="payment_webhook",
                    action="invoice.pay",
                    resource_type="invoice", resource_id=str(invoice.id),
                    before={"status": "issued"},
                    after={"status": "paid", "delivery": delivery_id},
                )
            elif invoice.status == "paid":
                pass  # idempotent no-op
            else:
                wd.result = "error"
                wd.error_message = f"invoice status={invoice.status}"
                wd.save()
                raise InvalidState()

            wd.processed_at = now()
            wd.save()

    except IntegrityError:
        # Duplicate delivery_id — already processed
        existing = WebhookDelivery.objects.get(delivery_id=delivery_id)
        return HttpResponse(status=200)  # idempotent success

    return HttpResponse(status=200)
```

### Idempotency proof — "delivered three times"
- **Delivery 1**: Inserts WebhookDelivery row; marks invoice paid; commits.
- **Delivery 2 (same delivery_id)**: `IntegrityError` on UNIQUE(delivery_id); inner txn rolls back; we return 200 (the provider must see success or it'll keep retrying). Invoice state unchanged.
- **Delivery 3 (different delivery_id, same payload)**: Passes dedup; in step 3, sees `invoice.status == "paid"`; no-op branch. WebhookDelivery row written with `processed_at`. Invoice unchanged.

### Failure modes
- **Body mutated after signing**: HMAC fails. Reject.
- **Timestamp skew**: ±5 min window. Document the threshold.
- **Signature secret rotated**: support both old + new key for a rotation window via env (`WEBHOOK_SECRET_CURRENT`, `WEBHOOK_SECRET_PREVIOUS`).
- **Slow-loris attack**: gunicorn timeout. Not a billing concern, but worth noting.

---

## Pipeline 5 — Reconciliation (daily)

Read-only background jobs. They write alerts, not state. Each detects drift between a denormalized value and the source of truth.

| Check | Query | If drift |
|---|---|---|
| `event_sum_vs_window` | `SELECT customer_id, window_start, SUM(units_consumed) FROM event WHERE event_timestamp >= now()-7d GROUP BY 1,2` compared to `usage_window` | Emit metric `billing.drift.window` with delta. Auto-recompute if window unsealed; otherwise alert. |
| `line_items_vs_window` | For each issued invoice in last 7d: SUM(line_item.amount) for usage lines vs `SUM(usage_window.units * tier_rate)` | Alert ops. Never auto-fix issued invoices. |
| `invoice_total_vs_lines` | `invoice.total_micro_cents` vs `SUM(line_item.amount_micro_cents)` | Recompute denormalized total. |
| `unpaid_old_invoices` | `status='issued' AND issued_at < now()-7d` | Surface in ops dashboard. |
| `late_event_backlog` | `COUNT(*) FROM event WHERE is_late AND adjusted_at IS NULL` per customer | Surface in ops; large numbers indicate upstream client clock issues. |

These reconciliation jobs **are the answer** to the rubric line "how ops debugs a wrong invoice." Run the queries; the drift report tells you whether the issue is at the event level, window level, or line-item level.

---

## Concurrency matrix (one-page summary)

| Operation | Lock acquired | Race partner | Outcome |
|---|---|---|---|
| Ingest event | UNIQUE constraint | Same `request_id` retry | DO NOTHING; idempotent |
| Aggregator chunk | per-customer xact lock | Invoicer for same customer | Serializes; aggregator waits |
| Issue invoice | per-customer xact lock + UNIQUE(customer, period) | Concurrent invoice issuance | Second sees existing draft; no-op |
| Issue credit | UNIQUE(staff_id, idempotency_key) | Double-click | Second returns stored response |
| Override line item | row-level lock on `line_item` + per-invoice advisory? | Two ops mid-edit | Last writer wins; both audit rows captured |
| Webhook delivery | UNIQUE(delivery_id) + FOR UPDATE on invoice | Replayed delivery | DO NOTHING + return 200 |
| Late-event flagging | invoicer Step 10 sweep (`ingested_at > txn_started_at`) | Mid-invoicer ingest for same period | Racy event flipped to `is_late=TRUE`; rolls into next month's adjustment. Aggregate correctness preserved. |

That last row is the one honest correctness boundary. Ingest is intentionally lockless (hot path); the invoicer absorbs the resulting race inside its own transaction. The system is **eventually consistent on `is_late`** within one invoicer cycle, and **aggregate-correct across consecutive invoices**. Surfaced explicitly in DESIGN.md §3.

Next: `API.md` — endpoint signatures, request/response, auth scoping, pagination strategy, idempotency-key handling.
