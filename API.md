# API Surface — Working Sketch

> Endpoint signatures, auth, tenant scoping, pagination, idempotency, error model. Backs DESIGN.md §5 (Threat model) and §6 (Trade-offs).

## Three entry points, three auth models

| Surface | Who | Auth | Where it lives | Sets |
|---|---|---|---|---|
| `/v1/*` (except `/v1/auth/*`) | Customer code OR customer-dashboard SPA | `Authorization: Bearer vk_live_...` API key **OR** httpOnly session cookie | `CustomerAuthentication` (DRF) | `request.customer` |
| `/v1/auth/*` | Customer-dashboard SPA pre-login | unauth (login endpoint) or session cookie | `SessionAuthentication` only | `request.customer_user` |
| `/ops/*` | Verita staff via ops SPA | Django staff session cookie + CSRF | DRF `SessionAuthentication` + `IsStaff` perm | `request.user` (Django `auth.User`) |
| `/webhooks/payments` | Payment processor (server-to-server) | HMAC signature in headers | view-local verifier (no tenant) | none |

Three auth backends. Distinct attributes (`request.customer` vs `request.customer_user` vs `request.user`). The router will refuse to mount a `/v1` view that doesn't subclass `CustomerScopedViewSet`. **Cross-contamination is structurally impossible**, not "we promised not to."

## Tenant scoping — where it actually lives

Two layers, defense in depth:

### Layer 1: Manager
```python
# apps/billing/managers.py
class CustomerScopedManager(models.Manager):
    def get_queryset(self):
        raise CustomerScopeMissing(
            "You must call .for_customer(customer) before querying. "
            "If you intentionally need cross-tenant, use .unsafe_all_tenants()."
        )

    def for_customer(self, customer):
        return super().get_queryset().filter(customer_id=customer.id)

    def unsafe_all_tenants(self):
        return super().get_queryset()
```

Every billing model (`Event`, `Invoice`, `UsageWindow`, `LineItem`, `Credit`) uses this manager as `objects`. Calling `Event.objects.all()` from any view → raises. The only paths to data are `for_customer(c)` (scoped) or `unsafe_all_tenants()` (explicit, grep-able, used only by ops + reconciliation jobs).

### Layer 2: Base ViewSet
```python
class CustomerScopedViewSet(viewsets.ModelViewSet):
    customer_scoped_model: type

    def get_queryset(self):
        return self.customer_scoped_model.objects.for_customer(self.request.customer)

    def perform_create(self, serializer):
        serializer.save(customer=self.request.customer)
```

All `/v1` views inherit from this. `get_queryset()` is never overridden in a way that drops the scope; reviewers can grep for `unsafe_all_tenants` and find every exception.

### Test surface
- `test_event_lookup_returns_404_for_other_tenant_id` — confirm enumerate-by-uuid is closed
- `test_manager_raises_when_called_without_scope` — confirm the safety net works
- `test_grep_unsafe_all_tenants_only_in_allowlist` — meta-test pinning which files may bypass

---

## `/v1` — customer-facing API

### `POST /v1/auth/login`
**Auth**: none. **Body**: `{ "email": "...", "password": "..." }`. **Response**: `Set-Cookie: session=...; HttpOnly; Secure; SameSite=Lax` and `{ user, customer }`. 401 on bad creds. Rate-limited per IP at 5/min via `django-ratelimit`.

### `POST /v1/auth/logout`
**Auth**: session cookie. Clears cookie. 204.

### `GET /v1/me`
**Auth**: API key OR session. **Response**:
```json
{
  "user": { "id": "...", "email": "...", "last_login_at": "..." },
  "customer": { "id": "...", "name": "...", "status": "active",
                "price_plan": { "id": "...", "name": "..." } }
}
```
Used by the SPA on load to bootstrap.

### `POST /v1/events`
**Auth**: API key (intended) or session (allowed for testing from dashboard). **Body**:
```json
{
  "events": [
    {
      "request_id": "req_8a7f...",
      "endpoint": "/api/foo",
      "units_consumed": 12,
      "timestamp": "2026-05-20T14:32:01Z"
    }
  ]
}
```
**Constraints**: ≤1000 events per batch, body ≤1 MiB. **Response 207**:
```json
{
  "results": [
    {"request_id": "req_8a7f...", "status": "accepted"},
    {"request_id": "req_dup_...", "status": "duplicate"}
  ]
}
```
**Idempotency**: per-event via `request_id` unique constraint. Replays are silent no-ops returning `duplicate`. See PIPELINE.md §1.

### `GET /v1/usage`
**Auth**: API key or session. **Query params**:
- `start` (ISO timestamp, inclusive, defaults to start of current period)
- `end` (ISO, exclusive, defaults to now)
- `api_key_id` (filter)
- `granularity` (`hour` | `day`, default `hour`)
- `cursor` (opaque)
- `limit` (default 100, max 1000)

**Response**:
```json
{
  "data": [
    { "window_start": "2026-05-20T14:00:00Z", "units_consumed": 1234, "event_count": 87 }
  ],
  "next_cursor": "eyJ0cyI6Li4uLCJpZCI6Li4ufQ"
}
```
**Data source**: `usage_window` (not raw `event`) — much faster, already aggregated. Raw event drill-down deferred to a separate endpoint if needed.

**Cursor encoding**: `base64({"ts": "...", "id": "..."})` — opaque to client, includes a checksum byte to detect tampering early.

### `GET /v1/invoices`
**Auth**: API key or session. **Query params**: `page` (default 1), `limit` (default 25, max 100), `status`. **Response**:
```json
{
  "data": [
    { "id": "...", "period_start": "...", "period_end": "...",
      "status": "paid", "total_micro_cents": 12500000000, "currency": "USD",
      "issued_at": "...", "paid_at": "..." }
  ],
  "page": 1, "limit": 25, "total": 14
}
```
**Why page+limit here**: at most ~12 invoices/year per customer. Deep-offset is irrelevant. Different from `/v1/usage` by design — see DESIGN.md §6 (Trade-offs).

### `GET /v1/invoices/{id}`
**Auth**: API key or session. **Tenant check**: implicit via manager — if the invoice belongs to another customer, queryset filter returns empty → 404 (NOT 403, to avoid confirming existence).

**Response**:
```json
{
  "id": "...", "period_start": "...", "period_end": "...",
  "status": "paid", "total_micro_cents": 12500000000, "currency": "USD",
  "issued_at": "...", "paid_at": "...",
  "line_items": [
    { "id": "...", "kind": "usage", "description": "Tier 1: 0–10,000 units (free)",
      "units": 10000, "unit_price_micro_cents": 0, "amount_micro_cents": 0 },
    { "id": "...", "kind": "usage", "description": "Tier 2: 90,000 units @ $0.001",
      "units": 90000, "unit_price_micro_cents": 100000, "amount_micro_cents": 9000000000 },
    { "id": "...", "kind": "credit_application", "description": "Credit: Customer service refund",
      "amount_micro_cents": -1000000000 }
  ]
}
```

---

## `/ops` — internal console API

All endpoints require `request.user.is_staff` (Django `auth.User`). CSRF token required on mutations. Audit log entry written in same transaction as every state change.

### `GET /ops/customers`
**Query**: `q` (search by name/email), `status`, `page`, `limit`. **Response**: paginated list with `id, name, billing_email, status, plan_name, current_period_units, anomaly_flag`.

`anomaly_flag` is computed at query time — see below.

### `GET /ops/customers/{id}`
**Response**: customer detail + recent invoices (last 12) + current-period usage chart data + anomaly signal:
```json
{
  "id": "...", "name": "...", "billing_email": "...", "status": "active",
  "price_plan": { "id": "...", "name": "...", "tiers": [...] },
  "current_period": {
    "units_consumed": 152340,
    "30d_daily_avg": 4523,
    "today_units": 48000,
    "anomaly": "10x_baseline"  // computed: today_units > 10 * 30d_daily_avg
  },
  "invoices": [ ... ],
  "api_keys": [
    { "id": "...", "prefix": "vk_live_a1b2", "name": "production",
      "created_at": "...", "last_used_at": "...", "revoked_at": null }
  ]
}
```
**Anomaly computation**: a single SQL query per request — `AVG(daily_units) FROM usage_window WHERE customer_id=? AND window_start >= now()-30d` grouped by day, compared to today's total. At 5,000 customers and ~720 windows/customer, the per-customer query is sub-50ms. At 10× we'd precompute baselines.

### `POST /ops/customers/{id}/credits`
**Headers**: `Idempotency-Key: <uuid-v4>` **required**.
**Body**:
```json
{ "amount_micro_cents": 5000000000, "reason": "Customer service refund — issue #1234" }
```
**Validation**:
- `amount_micro_cents > 0`
- `reason` not empty (audit requirement)
- Idempotency-Key matches existing? Return stored response.
- Same key + different body? 422 "Idempotency-Key reuse with different payload."

**Response 201**:
```json
{ "id": "...", "amount_micro_cents": 5000000000, "reason": "...",
  "applied_to_invoice_id": null, "created_at": "..." }
```
**Side effects** (in single transaction):
1. Insert `idempotency_key` row.
2. Insert `credit` row.
3. Insert `audit_log` row with `action='credit.issue'`, `before=null`, `after={amount, reason}`.

### `PATCH /ops/invoices/{invoice_id}/line-items/{line_item_id}`
**Auth**: staff. **Body**:
```json
{ "amount_micro_cents": 1000000000, "description": "Corrected: tier rate misapplied", "reason": "Audit found wrong tier" }
```
**Pre-conditions**:
- Invoice status must be `issued` (not `paid` — overrides on paid invoices require a credit instead). Or actually, allow override on paid invoices but require a stronger reason — TBD, will lock in DESIGN.md.
- Line item belongs to invoice (URL nesting enforces this).

**Side effects** (single transaction with `SELECT FOR UPDATE` on the line_item):
1. Capture `before = {amount_micro_cents, description}`.
2. Update `line_item.amount_micro_cents, description, overridden_at=now(), override_reason`.
3. Recompute `invoice.total_micro_cents` (sum of line items).
4. Insert `audit_log` with `action='line_item.override'`, `before`, `after`, `reason`.

---

## `/webhooks/payments`

Single endpoint. Stripe-style signature verification. See PIPELINE.md §4 for full algorithm. Auth and processing are independent: bad signature returns 401 without any DB write to the invoice. Replay returns 200 (idempotent).

Headers:
- `X-Signature: t=<unix_ts>,v1=<hex>`
- `X-Timestamp: <unix_ts>`
- `X-Delivery-ID: <unique>`

Body: `{ "invoice_id": "...", "amount_paid_micro_cents": ..., "currency": "USD" }`.

Response: `200 OK` on success or already-processed; `400` on stale timestamp; `401` on invalid signature.

---

## Pagination strategy

| Endpoint | Style | Why |
|---|---|---|
| `/v1/usage` | Cursor | Time-ordered, potentially deep history. Cursor avoids `OFFSET 50000` penalty at scale. |
| `/v1/invoices` | page+limit | Bounded set (~12/year). Page numbers are user-friendly. |
| `/ops/customers` | page+limit | 5,000 customers max. Sort options + jump-to-page beats cursor UX. |

Documented in DESIGN.md §6 as a deliberate non-uniform choice.

---

## Idempotency-Key handling

**Applies to**: `POST /ops/customers/{id}/credits` (mandatory). Optional but accepted on `POST /v1/events` if a client wants response-level idempotency on top of per-event request_id.

**Algorithm** (extracted into a `@idempotent` decorator):
```python
def handle_request_with_idempotency(request, view_fn):
    key = request.headers.get("Idempotency-Key")
    if not key:
        return view_fn(request)  # only required on credit-issuance

    scope = ("staff_id", request.user.id) if is_ops_view else \
            ("customer_id", request.customer.id)
    body_hash = sha256(canonical_json(request.data))

    with transaction.atomic():
        existing = IdempotencyKey.objects.filter(
            **{scope[0]: scope[1]}, key=key
        ).first()
        if existing:
            if existing.request_hash != body_hash:
                return JsonResponse({"error": {"code": "idempotency_conflict"}}, status=422)
            return JsonResponse(existing.response_body, status=existing.response_status)

        response = view_fn(request)
        IdempotencyKey.objects.create(
            **{scope[0]: scope[1]},
            key=key, method=request.method, path=request.path,
            request_hash=body_hash,
            response_status=response.status_code,
            response_body=response.data,
            expires_at=now() + timedelta(hours=24),
        )
        return response
```
**TTL**: 24h. Cleanup job hard-deletes expired rows nightly (the one table where deletion happens).

---

## Error model

Single shape, single content type (`application/json`):
```json
{
  "error": {
    "code": "machine_readable_slug",
    "message": "Human-readable explanation.",
    "details": { "field_name": "constraint violated" }
  }
}
```

| Status | Code | When |
|---|---|---|
| 400 | `invalid_request` | malformed body, missing required fields |
| 401 | `unauthenticated` | missing/invalid creds |
| 403 | `forbidden` | authenticated but wrong scope (e.g., customer hitting `/ops`) |
| 404 | `not_found` | resource missing OR cross-tenant access (intentionally indistinguishable) |
| 409 | `idempotency_conflict` | Idempotency-Key reuse with different body |
| 422 | `validation_failed` | semantic error (e.g., negative credit amount) |
| 429 | `rate_limited` | throttled (Retry-After header set) |
| 500 | `internal_error` | unexpected; includes a trace_id for support |

**Why 404 on cross-tenant**: a 403 confirms the resource exists. 404 doesn't. Stripe does the same.

---

## Rate limiting

`django-ratelimit` middleware, two scopes:
- `/v1/events`: 50 req/sec per API key (this is the data plane — generous)
- `/v1/auth/login`: 5/min per IP (anti-brute-force)
- `/v1/*` (other): 100/min per session
- `/ops/*`: not rate-limited (small population, trusted)
- `/webhooks/payments`: not rate-limited (verifier itself is the gate)

Anything denied returns 429 with `Retry-After` and the standard error body.

---

## OpenAPI / schema generation

`drf-spectacular` auto-generates OpenAPI 3.1 from DRF serializers + viewsets. Customer dashboard SPA can use `openapi-typescript` to generate TS types, ops SPA the same. Saves a lot of typing-by-hand and keeps the contract honest.

---

## What this document doesn't yet cover (in scope for THREATS.md next)

- Specific abuse scenarios per endpoint (hostile customer, hostile insider, compromised webhook)
- API key rotation flow
- Secret management (env vars, no secrets in repo, etc.)
- Audit log queries from the ops UI
