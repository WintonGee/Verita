# Verita — Usage Guide

A practical walkthrough of how to *use* the running system. For one-time setup
see [README.md](README.md); for the design reasoning see [DESIGN.md](DESIGN.md).

The system has three surfaces:

| Surface | Who | URL | Auth |
|---|---|---|---|
| **API** (`/v1`) | Customer code (server-to-server) | http://localhost:8000 | API key: `Authorization: Bearer vk_live_…` |
| **Customer dashboard** | A customer's people | http://localhost:5173 | Email + password (session cookie) |
| **Ops console** | Verita staff | http://localhost:5174 | Staff username + password |

Everything money is **integer micro-cents** (1 unit = $1e-8; $1.00 = 100,000,000).

---

## 0. Start it and load demo data

```bash
cp .env.example .env
docker compose up -d --wait                 # DB + API + cron + both SPAs, blocks until ready
docker compose run --rm backend python manage.py seed --customers=5 --days=45
docker compose run --rm backend python manage.py aggregate_events --catch-up
docker compose run --rm backend python manage.py issue_invoices
```

`docker compose up` serves both SPAs (customer-web :5173, ops-web :5174). For
frontend hot-reload development you can instead run one directly:
`cd frontend/customer-web && npm install && npm run dev`.

`seed` prints the credentials you'll use:

```
ops staff login: ops / ops-pass-123
  Acme Corp 1
    login: user1@example.com  /  password123
    api key: vk_live_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Interactive API docs (the customer `/v1` surface): http://localhost:8000/api/docs/

---

## 1. Sending usage as a customer (the API)

Set your key from the seed output:

```bash
KEY=vk_live_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Ingest events — batched and idempotent
`request_id` is your idempotency key. Re-sending the same one is a safe no-op
(reported as `"duplicate"`), so retries never double-bill.

```bash
curl -s -X POST http://localhost:8000/v1/events \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"events":[
    {"request_id":"req-001","endpoint":"/v1/infer","units_consumed":12,"timestamp":"2026-05-21T10:00:00Z"},
    {"request_id":"req-002","endpoint":"/v1/search","units_consumed":3,"timestamp":"2026-05-21T10:00:05Z"},
    {"request_id":"req-001","endpoint":"/v1/infer","units_consumed":12,"timestamp":"2026-05-21T10:00:00Z"}
  ]}'
# → results: req-001 accepted, req-002 accepted, req-001 duplicate
```

Limits: ≤1000 events/batch; `units_consumed` ≥ 0; timestamps may be in the past
(backfill is fine) but not >5 min in the future.

### Query your usage — by time bucket, filterable
```bash
# daily buckets for the current month
curl -s -H "Authorization: Bearer $KEY" \
  "http://localhost:8000/v1/usage?granularity=day&start=2026-05-01T00:00:00Z"

# filter to one API key, hour granularity, paginate with the returned cursor
curl -s -H "Authorization: Bearer $KEY" \
  "http://localhost:8000/v1/usage?granularity=hour&api_key_id=<uuid>&limit=50"
```

### See your invoices
```bash
curl -s -H "Authorization: Bearer $KEY" "http://localhost:8000/v1/invoices"
curl -s -H "Authorization: Bearer $KEY" "http://localhost:8000/v1/invoices/<invoice_id>"
```

> Note: events you just sent show up in `usage`/invoices only after the
> aggregator runs. The hourly cron does this automatically; to see it
> immediately, run `aggregate_events --catch-up` (see §5).

---

## 2. Customer dashboard (http://localhost:5173)

1. **Sign in** with a customer login (e.g. `user1@example.com` / `password123`).
2. **Dashboard** — current-period usage as a daily bar chart, a "units consumed
   this month" tile, and your invoice list.
3. **Invoice detail** — click *View* on any invoice to see the tiered line-item
   breakdown (free tier, then each priced tier, plus any credits/adjustments).

It's read-only: customers view usage and invoices; they don't move money.

---

## 3. Ops console (http://localhost:5174)

Sign in as `ops` / `ops-pass-123`.

- **Customers** — searchable, filterable list. Click *Open* for detail.
- **Customer detail** — current-period card (today's units vs the 30-day daily
  average) with a ⚠ **anomaly badge** when today exceeds 10× the baseline; a
  30-day usage chart; the invoice list with line items; and API keys.
- **Issue credit** — opens a modal (see §4).
- **Override** — next to each invoice line item (see §4).

Every credit and override is **money-moving**, so each one: shows a confirmation
restating the exact amount, carries an auto-generated **Idempotency-Key** (so a
double-click or retry can't double-apply), and writes an **immutable audit row**
(actor, before/after, reason) in the same transaction as the change.

---

## 4. Ops cookbook

### Issue a credit
Console: **Issue credit** → enter dollar amount + a reason (≥10 chars) → the
confirm button restates the amount → **Confirm**. The credit is applied as a
negative line item on the customer's next invoice.

API equivalent (note the required `Idempotency-Key`):
```bash
# log in as staff, capturing the session + CSRF cookie
curl -s -c /tmp/ops.txt -X POST http://localhost:8000/ops/auth/login \
  -H "Content-Type: application/json" -d '{"username":"ops","password":"ops-pass-123"}'
curl -s -b /tmp/ops.txt -c /tmp/ops.txt http://localhost:8000/ops/auth/me >/dev/null  # sets csrftoken
CSRF=$(grep csrftoken /tmp/ops.txt | awk '{print $7}')

curl -s -b /tmp/ops.txt -X POST \
  "http://localhost:8000/ops/customers/<customer_id>/credits" \
  -H "Content-Type: application/json" -H "X-CSRFToken: $CSRF" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"amount_micro_cents":500000000,"reason":"Goodwill credit for outage"}'
# 500000000 micro-cents = $5.00
```

### Override an invoice line item
Console: **Override** on a line item → new amount + reason → confirm (shows the
diff). Recomputes the invoice total and audits before/after. **Blocked on paid
invoices** — correct a paid invoice with a credit instead.

### Debug a "wrong invoice"
Use the reconciliation report — it walks the same three layers the data flows
through, so it localizes where a number diverged:
```bash
docker compose run --rm backend python manage.py run_reconciliation
```
It reports: window total vs raw-event sum, invoice total vs line-item sum, and
stuck drafts. Then:
- mismatch at **events ↔ window** → an aggregation issue;
- mismatch at **window ↔ line items** → an issuance issue;
- genuine overcharge → resolve with a **credit** (audited), don't silently edit
  a sealed invoice.

### Mark an invoice paid (payment webhook)
There is intentionally **no ops "mark paid" button** — only the signed webhook
transitions `issued → paid`. To simulate a payment, save this as
`send_payment.py` and run it (`pip install requests` if needed):

```python
import hmac, hashlib, time, json, uuid, os, sys, requests
secret = os.environ.get("WEBHOOK_SECRET_CURRENT", "CHANGE-ME-dev-only-not-a-real-secret")
invoice_id, amount = sys.argv[1], int(sys.argv[2])  # amount in micro-cents = invoice total
ts = str(int(time.time()))
body = json.dumps({"invoice_id": invoice_id, "amount_paid_micro_cents": amount, "currency": "USD"})
sig = hmac.new(secret.encode(), f"{ts}.".encode() + body.encode(), hashlib.sha256).hexdigest()
r = requests.post("http://localhost:8000/webhooks/payments", data=body, headers={
    "Content-Type": "application/json",
    "X-Signature": f"t={ts},v1={sig}",
    "X-Delivery-ID": str(uuid.uuid4()),
})
print(r.status_code, r.text)
```
```bash
export $(grep WEBHOOK_SECRET_CURRENT .env)   # sign with the secret the API uses
python send_payment.py <invoice_id> <invoice_total_micro_cents>
```
The handler verifies the HMAC, rejects stale timestamps (±5 min) and amount
mismatches, and is replay-safe (re-sending the same `X-Delivery-ID` is a no-op).

---

## 5. Running the billing pipeline by hand

Cron runs these automatically; you can also invoke any of them directly:

| Command | What it does |
|---|---|
| `python manage.py aggregate_events [--catch-up]` | Events → hourly `usage_window`s. `--catch-up` ignores the 5-min edge delay (use right after seeding). |
| `python manage.py issue_invoices [--customer <id>]` | Issues invoices for the **previous calendar month** for all (or one) customer. |
| `python manage.py run_reconciliation` | Read-only drift report (the debugging tool above). |
| `python manage.py cleanup_idempotency_keys` | Prunes expired idempotency keys (the one table where deletion is allowed). |
| `python manage.py seed --customers N --days D [--reset]` | (Re)generate demo data. |

Prefix each with `docker compose run --rm backend `.

---

## 6. Running the tests

```bash
docker compose run --rm backend python -m pytest          # all (~106)
docker compose run --rm backend python -m pytest -m concurrency   # the thread-race tests
docker compose run --rm backend python -m pytest tests/test_pricing.py -v
```

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `aggregate_events` reports 0 windows right after seeding | The aggregator skips events ingested in the last 5 min. Run `aggregate_events --catch-up`. |
| `issue_invoices` made $0 invoices | It bills the **previous** calendar month; seeded data may be in the current month. That's expected — usage still shows on the dashboard. |
| Ops mutation fails with a CSRF error | The SPA origin must be in `CSRF_TRUSTED_ORIGINS` (defaults cover `localhost:5173/5174`). Direct API calls must send the `X-CSRFToken` header (see §4). |
| Webhook returns 401 / 400 | 401 = signature mismatch (wrong `WEBHOOK_SECRET_CURRENT` or body altered after signing); 400 = timestamp outside ±5 min. |
| Webhook returns 422 | `amount_paid_micro_cents` must equal the invoice total. |
| Customer dashboard / ops console can't reach the API | Make sure the API is up (`curl localhost:8000/healthz`) and the Vite dev server is running; it proxies `/v1` and `/ops` to `:8000`. |
| Login rejected after many tries | Login is rate-limited to 5/min/IP (both customer and staff). Wait a minute. |

---

## 8. Where things live

```
backend/apps/tenancy   customers, customer users, API keys, sessions
backend/apps/billing   pricing, events, windows, invoices, credits;
                       aggregator / invoicer / reconciliation
backend/apps/audit     immutable audit log, webhook deliveries, idempotency keys
backend/apps/api       /v1 (customer), /ops (staff), /webhooks
frontend/customer-web  customer dashboard SPA
frontend/ops-web       ops console SPA
```
