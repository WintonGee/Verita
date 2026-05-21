// Hand-written interfaces mirroring the API contract (API.md).

export interface PricePlan {
  id: string;
  name: string;
}

export interface User {
  id: string;
  email: string;
  last_login_at: string | null;
}

export type CustomerStatus = string; // e.g. "active", "suspended"

export interface Customer {
  id: string;
  name: string;
  status: CustomerStatus;
  price_plan: PricePlan;
}

/** Shape returned by both POST /v1/auth/login and GET /v1/me. */
export interface MeResponse {
  user: User;
  customer: Customer;
}

export interface UsageWindow {
  window_start: string; // ISO timestamp
  units_consumed: number;
  event_count: number;
}

export interface UsageResponse {
  data: UsageWindow[];
  next_cursor: string | null;
}

export type InvoiceStatus = string; // e.g. "draft", "issued", "paid", "void"

export interface InvoiceSummary {
  id: string;
  period_start: string;
  period_end: string;
  status: InvoiceStatus;
  total_micro_cents: number;
  currency: string;
  issued_at: string | null;
  paid_at: string | null;
}

export interface InvoiceListResponse {
  data: InvoiceSummary[];
  page: number;
  limit: number;
  total: number;
}

export type LineItemKind = string; // e.g. "usage", "credit_application"

export interface LineItem {
  id: string;
  kind: LineItemKind;
  description: string;
  units: number | null;
  unit_price_micro_cents: number | null;
  amount_micro_cents: number;
  tier_ordinal: number | null;
  overridden_at: string | null;
  override_reason: string | null;
}

export interface InvoiceDetail extends InvoiceSummary {
  line_items: LineItem[];
}

/** Standard API error envelope: {"error":{"code","message"}}. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}
