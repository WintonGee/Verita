# Front-End — Working Sketch

> Two SPAs in one repo. Minimal, functional, money-moving UX. Brief: "We are evaluating operational UX clarity and safety, not frontend polish." Backs the API & frontend craft rubric (13%, combined with API quality).

## Repo layout

```
/
├── backend/                  Django + DRF (the API)
│   └── apps/                 (Django apps, conventional location)
├── frontend/
│   ├── customer-web/         SPA #1: customer dashboard
│   │   ├── src/
│   │   ├── package.json
│   │   └── vite.config.ts
│   └── ops-web/              SPA #2: internal console
│       ├── src/
│       └── ...
├── packages/
│   └── api-types/            generated TS types from drf-spectacular OpenAPI
├── docker-compose.yml
├── DESIGN.md
└── README.md
```

Note: `frontend/` not `apps/` for the SPAs, because Django convention reserves `apps/` for backend apps (where Django apps actually live, under `backend/apps/`).

`api-types` is generated via `npm run generate:types` → `openapi-typescript backend/openapi.json -o packages/api-types/index.ts`. Both SPAs import from `@verita/api-types`. Keeps frontend honest with backend.

## Shared stack

- **Vite + React 18 + TypeScript**
- **TanStack Query (React Query)** — caching, retries, optimistic state. Critical for the loading/error story.
- **react-hook-form + zod** — forms with type-safe validation. Money inputs use a custom integer-only field.
- **Recharts** — the only chart we need (current-period usage). Lightweight.
- **Tailwind CSS** — no design system; utility classes. Avoids "spent 2 hours on a component library setup."
- **No router gymnastics** — `react-router-dom` v6, ~3 routes per SPA.

Why not Next.js / Remix: SSR adds complexity without payoff for an authenticated dashboard. Brief says minimal.

## SPA 1 — Customer Dashboard

### Routes
- `/login` — email + password form
- `/` — current-period usage (chart) + invoice list
- `/invoices/:id` — invoice detail with line items
- `/keys` — list of API keys (not in MVP scope; would surface if time permits)

### Auth flow
1. `GET /v1/me` on app load. If 401: redirect to `/login`.
2. `POST /v1/auth/login { email, password }` → server sets `Set-Cookie: session=...; HttpOnly; Secure; SameSite=Lax`. Frontend never sees the cookie.
3. All API calls go through a shared `apiClient` that handles 401 → redirect to login.

### Home page (`/`)
- Header: customer name, plan name, "Sign out" button.
- Chart: current calendar month usage by day. Data from `GET /v1/usage?granularity=day&start=<period_start>`. Recharts bar chart, axis labels in plain ASCII (no fancy fonts).
- "Current period estimate" tile: total units consumed, estimated invoice amount (sum of `units × tier_rate`).
- Invoice list table: `period`, `status` badge, `total`, "View" link. Pages of 25.

### Invoice detail (`/invoices/:id`)
- Header: invoice id, period, status, total.
- Line items table: kind, description, units, unit price, amount.
- Loading state: skeleton rows. Error state: red banner + "Retry" button. 404 → "Invoice not found" (we never confirm whether it exists across tenants).

### Loading / error patterns
- React Query: every query has explicit `isLoading`, `isError`, `error` handling.
- A shared `<QueryBoundary>` component wraps every fetch: spinner during load, error card with `error.code` and `error.message` (plus a "Retry" button) on failure.
- No "empty success" silently rendering blank — if data is `[]`, show "No invoices yet."

## SPA 2 — Ops Console

### Routes
- `/login` — Django staff login (uses Django's session auth; the SPA just shows the form)
- `/customers` — list with search, status filter, anomaly badge
- `/customers/:id` — detail (usage, invoices, API keys, issue credit, override line item)
- `/audit` — global audit log feed (nice to have)

### Auth flow
- Same shape as customer SPA, but `/ops/auth/login` instead. Session cookie scoped to a separate cookie name (`ops_session`) so customers and staff can't co-exist in the same browser session.
- CSRF: `csrftoken` cookie set on login; every POST/PATCH/DELETE includes `X-CSRFToken` header. React app reads cookie via Django's standard pattern.

### Customer detail (`/customers/:id`)
- Header: name, billing email, status, plan.
- **Current period card**: today's units, 30d daily average, anomaly badge if today > 10× avg ("⚠ 12× baseline").
- **Usage chart**: same as customer view, but ops can see further back.
- **Invoices table**: status, total, "Open" link.
- **API keys table**: prefix, name, created_at, last_used_at, revoked_at, "Revoke" button.
- **Issue credit button** → opens modal (see below).

### Issue credit modal — the money-moving UI pattern

The rubric: "money-moving UI has confirmation + idempotency token."

```
┌─────────────────────────────────────────┐
│  Issue credit to Acme Corp              │
├─────────────────────────────────────────┤
│  Amount: $ [    0.00 ]                  │
│  Reason: [                          ]   │
│                                         │
│  ──────────────────────────────         │
│  This will:                             │
│  • Create a credit of $X.XX             │
│  • Apply to the next invoice            │
│  • Write an audit log entry             │
│                                         │
│  Idempotency-Key: 8a7f-...  (auto)      │
│                                         │
│  [ Cancel ]   [ Confirm — issue $X.XX ] │
└─────────────────────────────────────────┘
```

Specifics:
1. Money input enforces integer cents (no float arithmetic in JS). Display as dollars, store/send as `amount_micro_cents`.
2. Reason: required, min 10 chars (matches backend validation).
3. Idempotency-Key: generated via `crypto.randomUUID()` on modal open. Locked for the lifetime of the modal. If user retries (network failure), same key is used → server returns the original response.
4. Confirm button shows the dollar amount being issued (so you can't fat-finger and miss it).
5. After submit, button is disabled until response. On success: toast with audit row link; modal closes.
6. On error: shows error.message; the same Idempotency-Key is reused on retry.

### Line-item override modal
Same pattern. Shows current amount, asks for new amount + reason (≥10 chars), generates an Idempotency-Key, displays the diff in the confirm button: `Change line item from $1,000.00 to $500.00 (–$500.00)`.

## Money input — the integer-cents discipline

```tsx
// MoneyInput.tsx
function MoneyInput({ value, onChange }: { value: number /* micro_cents */, onChange: (v: number) => void }) {
  const [text, setText] = useState((value / 1e8).toFixed(2));
  return (
    <input
      type="text"
      inputMode="decimal"
      value={text}
      onChange={(e) => {
        const t = e.target.value.replace(/[^0-9.]/g, '');
        setText(t);
        const dollars = parseFloat(t || '0');
        if (!Number.isNaN(dollars)) {
          onChange(Math.round(dollars * 1e8));  // → micro_cents, integer
        }
      }}
    />
  );
}
```

No float arithmetic. Display is decimal-string-only. The internal value is always integer micro-cents.

## What we deliberately skip

| Skipped | Why |
|---|---|
| Component library (Chakra / MUI) | Tailwind utilities are enough; library setup eats time |
| Dark mode | Polish, not function |
| Animations beyond opacity transitions | Polish |
| Code-splitting / route lazy-loading | Both SPAs are small enough |
| i18n | English only |
| Mobile responsive beyond viewport-tags | Not a phone-first product |
| Storybook | We're not building a design system |
| Visual regression tests | Not the eval criteria |

## State-management decisions

- Server state: TanStack Query (with default 30s staleTime; `/v1/me` is `staleTime: Infinity` until logout).
- Form state: react-hook-form per-form (local).
- Auth state: derived from `/v1/me` query result.
- Global UI state (toasts): tiny zustand store, ~30 lines.

No Redux. No context juggling.

## API client patterns

```ts
// apiClient.ts
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCookie('csrftoken') ?? '',
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  if (res.status === 401) {
    window.location.href = '/login';
    throw new ApiError(401, 'unauthenticated');
  }
  const body = await res.json();
  if (!res.ok) throw new ApiError(res.status, body.error.code, body.error.message);
  return body;
}
```

Two patterns ensured:
1. CSRF token attached to every request (a no-op if the server doesn't require it).
2. 401 always boots to login. Idempotency-keyed mutations are explicit and pass the key in headers.

## Loading & error states — concrete examples

| Surface | Loading | Empty | Error |
|---|---|---|---|
| Usage chart | skeleton bars | "No usage yet this period." | red card: "Couldn't load usage. [Retry]" |
| Invoice list | skeleton rows | "No invoices yet." | red card with retry |
| Invoice detail | skeleton page | n/a (single resource) | "This invoice doesn't exist or you don't have access." |
| Customer list (ops) | skeleton rows | "No customers." | red card with retry |
| Credit submit | disabled button + spinner | n/a | inline error under form; key preserved for retry |

These aren't afterthoughts — the rubric specifically calls them out.

## Build & deploy

Both SPAs build to static `dist/` directories. In production, Django serves them via `whitenoise` or nginx. In dev, each SPA runs on its own Vite port (5173 customer, 5174 ops); a Django CORS allowlist permits both. `docker-compose up` brings up: Django, Postgres, a cron sidecar (runs the scheduled tasks via `python manage.py`), customer-web, ops-web. No Redis, no Celery.
