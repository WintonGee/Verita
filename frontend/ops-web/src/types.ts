// Hand-written TS interfaces for the /ops API responses.
// Money fields are always integer micro_cents (1 unit = $1e-8). Never floats.

export interface User {
  username: string;
  email: string;
}

export interface MeResponse {
  user: User;
}

export interface LoginResponse {
  user: User;
}

export type CustomerStatus = 'active' | 'suspended' | 'closed' | string;

// Row shape from GET /ops/customers
export interface CustomerListItem {
  id: string;
  name: string;
  billing_email: string;
  status: CustomerStatus;
  plan_name: string;
  created_at: string;
}

export interface CustomerListResponse {
  data: CustomerListItem[];
  page: number;
  limit: number;
  total: number;
}

export interface PricePlan {
  id: string;
  name: string;
}

export interface CurrentPeriod {
  today_units: number;
  thirty_day_daily_avg: number;
  anomaly: boolean;
  multiplier_threshold: number;
}

export type InvoiceStatus = 'draft' | 'issued' | 'paid' | 'void' | string;

export interface InvoiceLineItem {
  id: string;
  description: string;
  amount_micro_cents: number;
  // present once an override has been applied
  overridden_at?: string | null;
  override_reason?: string | null;
}

export interface Invoice {
  id: string;
  period_start: string;
  period_end: string;
  status: InvoiceStatus;
  total_micro_cents: number;
  currency: string;
  issued_at: string | null;
  paid_at: string | null;
  // Not part of the documented GET /ops/customers/{id} contract; present only
  // if the backend chooses to embed line items. The Override action is gated on
  // this being available. See report: documented as an assumption/gap.
  line_items?: InvoiceLineItem[];
}

export interface ApiKey {
  id: string;
  prefix: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface CustomerDetail {
  id: string;
  name: string;
  billing_email: string;
  status: CustomerStatus;
  price_plan: PricePlan;
  current_period: CurrentPeriod;
  invoices: Invoice[];
  api_keys: ApiKey[];
}

// POST /ops/customers/{id}/credits → 201
export interface CreditResponse {
  id: string;
  amount_micro_cents: number;
  reason: string;
  applied_to_invoice_id: string | null;
  created_at: string;
}

// PATCH /ops/invoices/{invoice_id}/line-items/{line_item_id} → 200
export interface LineItemOverrideResponse {
  id: string;
  amount_micro_cents: number;
  description: string;
  overridden_at: string;
  override_reason: string;
  invoice_total_micro_cents: number;
}

// Standard error envelope: {"error": {"code", "message"}}
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}
