# Threat Model — Working Sketch

> Three actors × concrete abuse scenarios × the specific mechanism that stops each. Backs DESIGN.md §5. The rubric calls out: "threat model is specific, not generic OWASP-ese" — every entry below cites a line of code, a constraint, or a trigger.

## Trust boundaries

```
        ┌────────────────────────────────────────────────┐
        │            Untrusted (internet)                │
        │  ┌────────────────────┐  ┌──────────────────┐ │
        │  │ Customer code      │  │ Customer-dashboard│ │
        │  │ (uses API key)     │  │ SPA (browser)     │ │
        │  └────────────────────┘  └──────────────────┘ │
        └─────────────┬───────────────────┬──────────────┘
                      │ HTTPS              │ HTTPS + cookie
                      ▼                    ▼
           ┌──────────────────────────────────────┐
           │ Edge: TLS termination, WAF, rate-lim │
           ├──────────────────────────────────────┤
           │ /v1 — CustomerAuthentication         │
           │       sets request.customer          │
           │ /ops — StaffSession + CSRF + IsStaff │
           │ /webhooks — HMAC verify, no tenant   │
           ├──────────────────────────────────────┤
           │ DRF views; tenant scope enforced at  │
           │ Manager.for_customer() (never view)  │
           ├──────────────────────────────────────┤
           │ Postgres (app_role: no UPDATE on     │
           │ audit_log; migrations as separate    │
           │ role)                                │
           └──────────────────────────────────────┘
                      ▲
                      │ webhook (signed)
        ┌─────────────┴─────────────────┐
        │ Payment processor (semi-trust)│
        └───────────────────────────────┘
```

Trust assumptions:
- **TLS in front of every endpoint** — assumed by the platform; out-of-scope to implement but mentioned in deploy notes.
- **Postgres + app_role** is trusted. **Migrator role** is more trusted. Anyone with `migrator_role` access can mutate audit log — that's a deliberate concession; rotated secret, MFA, not in app stack.
- **Payment processor** is semi-trusted: their HMAC secret can leak, so the design assumes it might.

---

## Actor 1 — Hostile customer

The customer is a legitimate paying tenant. They might be curious, malicious, or both. They have a valid API key.

| Attack | Worst case | What stops it |
|---|---|---|
| Read another customer's invoice by guessing UUID | Cross-tenant data leak | `CustomerScopedManager.for_customer()` filters by `customer_id` at the queryset layer. Even with a known UUID, returns `404 not_found` — same response as a non-existent ID, so existence isn't confirmed. Test: `test_invoice_404_for_other_tenant`. |
| List events with constructed `customer_id` query param | Cross-tenant leak | View ignores any client-supplied `customer_id`; manager pulls it from `request.customer` exclusively. Manager raises `CustomerScopeMissing` if not set. |
| Replay a stolen event payload | Double-billing themselves (self-harm, but creates noise) | `UNIQUE(request_id)` on `event` makes replays a no-op. Customer can't double-bill themselves. |
| Send `units_consumed = -1000000` to deflate their own bill | Underbilling | `CHECK (units_consumed >= 0)` blocks at insert. |
| Send an event with `customer_id` field of another tenant in the JSON body | Cross-tenant injection | The `customer_id` field is **not in the event ingestion schema**. It's derived from `request.customer` server-side. Any field client tries to set is dropped by DRF serializer. |
| Forge a payment webhook to mark own invoice paid | Free service | HMAC-SHA256 over `timestamp.body` with shared secret. Customer doesn't have the secret (never leaves env). Even if they did, `X-Timestamp` outside ±5min is rejected. |
| Brute-force their own login (compromised credentials) | Account takeover | Rate-limit `5/min/IP` on `/v1/auth/login`. argon2id hashing means stolen DB doesn't give passwords. **Out-of-scope but flagged**: account lockout, MFA. |
| Brute-force API key | Key guessing | The key secret is 16 random bytes = 128 bits of entropy (32 hex chars). Postgres can't be probed fast enough; rate-limited at edge. Adversary needs ~2¹²⁸ requests. |
| XSS-payload injection in `endpoint` field | Stored XSS in ops console | `endpoint` is stored as raw varchar but rendered via React (escapes by default). No `dangerouslySetInnerHTML` anywhere. CSP header forbids inline scripts. |
| Massive event volume to harm our throughput | DoS | Per-API-key rate limit (50/s on `/v1/events`). Exceeding returns 429. WAF can also throttle at edge. **The customer pays for accepted events** — economic disincentive. |
| Set `event_timestamp` in the future to game seal windows | Skew invoice period | Reject events with `event_timestamp > now() + 5min` at ingest. Accept past timestamps freely (backfill is a legitimate use case). |
| Reverse-engineer cursor token to enumerate other tenants | Cross-tenant via cursor | Cursor encodes `(window_start, id)` but the decoded values are still passed through the customer-scoped manager. Even a forged cursor pointing at another tenant's window returns empty results. |
| SQL injection via crafted `endpoint` field | DB compromise | Django ORM parameterizes everything. No raw SQL in user-facing paths. Periodic `grep` for `.raw(` and `cursor.execute(` confined to admin tools. |

---

## Actor 2 — Hostile internal user (ops staff)

A staff member with valid ops credentials. The most dangerous actor because they have *intentional* write access.

| Attack | Worst case | What stops it (or what catches it) |
|---|---|---|
| Issue a $10,000 credit to a friend | Direct financial loss | `audit_log` row written **in the same transaction** as the credit. Captures: actor email, IP, before, after, reason (required). Daily report alerts on credits > $threshold. Even if the credit succeeds, it's not deniable. |
| Issue many small credits below alert threshold | Slow embezzlement | Audit log also feeds a weekly aggregate-by-actor report: "Top 10 staff by credit-amount this week." Patterns surface even when individuals don't. |
| Override invoice line item to $0 | Free service for chosen customer | Same as credits: audit row with before/after/reason captured in same txn. UI requires reason ≥10 chars. PATCH endpoint reads original from row, writes new + audit atomically. |
| Try to UPDATE an audit row to cover tracks | Erasing the evidence | Postgres trigger `audit_log_immutable`: `BEFORE UPDATE OR DELETE ON audit_log` → `RAISE EXCEPTION`. Belt-and-suspenders: `app_role` lacks UPDATE/DELETE grants. Even SQL-injected as `app_role`, mutation fails. The migrator role *can* but is locked behind a separate secret + MFA gate (out-of-scope but documented). |
| DROP TABLE audit_log via SQL injection | Erase everything | App role has no DROP grant. Migration role does, but lives outside the app. SQL injection in app code can't escalate. |
| Manually mark an invoice paid (no real payment) | Customer thinks they paid; we lose money or get audited | The `/ops` API has no "mark paid" endpoint. Only the webhook handler transitions `issued → paid`. To manually mark paid, you'd need direct DB write — which is outside the app role. (Trade-off: gives up an ops-recovery escape hatch. Documented.) |
| Quietly revoke a customer's API key | Customer outage / extortion | `api_key.revoked_at` change captured by audit. Ops detail view of customer shows revocation history with actor. |
| Read all customers' usage | Intentional — that's the job | Reduce blast via Django Groups: `billing-readonly` group can view, `billing-admin` can mutate. Out-of-scope for the take-home (will mention in "what's next"); for v1 all staff are full-access but every action is audited. |
| Bulk-export PII to laptop | Data exfiltration | Out-of-scope (DLP). Flagged in DESIGN.md "didn't build." |
| Subvert a deploy to introduce a backdoor | Total compromise | Out-of-scope (supply chain). Mentioned for completeness. |

**The pattern**: every mutating ops action requires `reason` (text, ≥10 chars), writes an audit row in the same txn, alerts on amount/frequency thresholds. We can't *prevent* an insider with valid creds from acting; we make their actions visible and undeniable.

---

## Actor 3 — Compromised webhook source

Assume the payment processor's HMAC secret has leaked (incident on their side, or ours via env-var exposure). The attacker can now sign arbitrary payloads.

| Attack | Worst case | What limits the blast |
|---|---|---|
| Mark any invoice as paid | Direct financial loss equal to fake-paid amount | The webhook handler is **the only** path that transitions `issued → paid`. Limiting that one transition means we can rotate the secret, audit the period, and reverse via credit. |
| Mark already-paid invoices paid again | Noise only | `invoice.status == "paid"` branch is a no-op. Duplicate `delivery_id` returns 200 with no DB change. |
| Replay a real old delivery with fresh signature | Reactivate cleanup-marked invoice | `X-Timestamp` window of ±5min means signatures expire 5min after they're minted. Old captured signatures can't be replayed. New signatures require the secret. |
| Mark voided invoices paid | Confusion | Webhook handler's state machine: only `issued → paid` is allowed; other transitions return error. Audit row captures the rejected attempt. |
| Change invoice amount via webhook | Wrong amount marked paid | The webhook payload's `amount_paid` is **not used** to mutate `invoice.total`. We only update `status`, `paid_at`, `payment_delivery_id`. The amount is logged for reconciliation but doesn't overwrite the contract amount. |
| Spam webhook to flood logs | Disk fill | `WebhookDelivery` rows are bounded by the rate of unique `delivery_id`s. Bad signatures rejected before write. Stale-timestamp rejection also pre-DB. |
| Rotate to attacker's new secret | Indefinite compromise | Secret rotation requires changes to env vars; standard secret-management practice (vault + redeploy). Hot-reload via SIGHUP not supported in v1. |

**Containment / detection**:
- Daily check: `SELECT * FROM webhook_delivery WHERE result='accepted' AND received_at > now() - interval '1 day'` should match the payment processor's outbound count. Drift = compromise.
- All `invoice.pay` audit rows include `payment_delivery_id`. Cross-reference with the processor's records.
- Secret rotation playbook: env-var update + `WEBHOOK_SECRET_CURRENT` + `WEBHOOK_SECRET_PREVIOUS` overlap window so in-flight deliveries don't bounce.

---

## Specific abuse scenarios called out in the rubric

The brief lists six concrete scenarios. Mapping them to the table entries above:

| Brief item | Where it's addressed |
|---|---|
| Cross-tenant access | Actor 1, rows 1-2; tenant-scoped manager |
| Replay attacks | Actor 1, row 3 (events); Actor 3, row 3 (webhooks); login rate-limit |
| Operator misuse | Actor 2, all rows; audit immutability via trigger + grants |
| Invoice tampering | Actor 2, rows 3-7; manual-paid disallowed by design |
| Credential leakage | API keys: SHA-256 + salt + prefix, shown once. Customer passwords: argon2id. Webhook secret: env-only, rotation support. None in repo. Log scrubber redacts `Authorization` headers. |
| Duplicate financial actions | Credits: Idempotency-Key + UNIQUE(staff_id, key). Webhook: UNIQUE(delivery_id). Invoicer: per-customer advisory lock + UNIQUE(customer_id, period_start). |

---

## Secrets policy

| Secret | Where it lives | How it's rotated |
|---|---|---|
| `DATABASE_URL` | env (`.env` local, secrets manager in prod) | redeploy |
| `WEBHOOK_SECRET_CURRENT` / `WEBHOOK_SECRET_PREVIOUS` | env | overlap window: set previous to old, current to new, redeploy; after window, drop previous |
| `DJANGO_SECRET_KEY` | env | redeploy (cookie sessions invalidated; users re-login) |
| `CUSTOMER_USER_PASSWORD_HASH` (per row) | DB (argon2id) | individual via password reset flow |
| `API_KEY_HASH` (per row) | DB (SHA-256 + per-key salt) | revoke + reissue; plaintext never recoverable |

`.gitignore` includes `.env`, `*.env.local`, `secrets/`. Pre-commit hook scans staged files for high-entropy strings. None of this is novel — it's table stakes — but the writeup needs to name the practice rather than gesture.

---

## What's intentionally out of scope (will appear in DESIGN.md §7)

- Multi-region / disaster recovery
- TLS pinning
- WAF rules beyond rate limits
- DLP / data exfiltration prevention
- Insider-collusion mitigation (multiple-staff approval for large credits)
- Customer MFA
- Supply chain / dependency provenance
- Hardware security module for the webhook secret
- Field-level encryption at rest beyond Postgres's pgcrypto

Each is named so the reader can see we considered it.
