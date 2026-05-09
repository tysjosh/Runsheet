// ─── Commerce Types ──────────────────────────────────────────────────────────
// Shared TypeScript types mirroring the backend Pydantic models.

// ─── Enums ───────────────────────────────────────────────────────────────────

export type CustomerStatus = "active" | "archived" | "suspended";

export type AccountStatus = "active" | "suspended" | "closed";

export type CreditState =
  | "good_standing"
  | "on_hold"
  | "override_active"
  | "suspended";

export type AccountTier = "default" | "preferred" | "enterprise";

export type PricingScopeType = "account" | "tier" | "default";

export type InvoiceStatus =
  | "draft"
  | "open"
  | "partial"
  | "paid"
  | "overdue"
  | "void";

export type QBOPushState =
  | "pending"
  | "pushed"
  | "failed"
  | "retrying"
  | "dead_letter";

export type PaymentSource =
  | "stripe"
  | "qbo"
  | "manual"
  | "account_credit"
  | "void_cascade";

export type PaymentMethod =
  | "ach"
  | "wire"
  | "check"
  | "credit_card"
  | "credit_balance"
  | "cash";

export type PaymentStatus = "applied" | "reversed" | "pending";

export type InvoiceEventType =
  | "created"
  | "finalized"
  | "payment_applied"
  | "payment_reversed"
  | "voided"
  | "overdue_transition"
  | "qbo_push_attempted"
  | "qbo_push_succeeded"
  | "qbo_push_failed";

export type AccountEventType =
  | "created"
  | "credit_hold_applied"
  | "credit_hold_released"
  | "credit_override_applied"
  | "credit_override_expired"
  | "payment_applied"
  | "suspended"
  | "reactivated";

// ─── Models ──────────────────────────────────────────────────────────────────

export interface BillingAddress {
  line1: string;
  line2?: string;
  city: string;
  state: string;
  zip: string;
  country: string;
}

export interface Customer {
  customer_id: string;
  tenant_id: string;
  display_name: string;
  legal_name?: string;
  tax_id?: string;
  email?: string;
  phone?: string;
  status: CustomerStatus;
  account_ids: string[];
  tags: string[];
  created_at: string;
  updated_at: string;
}

export interface Account {
  account_id: string;
  tenant_id: string;
  customer_id: string;
  display_name: string;
  status: AccountStatus;
  tier: AccountTier;
  credit_state: CreditState;
  credit_limit_cents: number;
  open_balance_cents: number;
  credit_balance_cents: number;
  net_terms_days: number;
  billing_address?: BillingAddress;
  credit_override_expires_at?: string;
  credit_override_reason?: string;
  created_at: string;
  updated_at: string;
}

export interface PricingRule {
  rule_id: string;
  product_code: string;
  product_name?: string;
  scope_type: PricingScopeType;
  scope_id?: string;
  unit_price_cents: number;
  min_quantity?: number;
  max_quantity?: number;
  effective_from: string;
  effective_to?: string;
  priority: number;
}

export interface PriceBook {
  price_book_id: string;
  tenant_id: string;
  name: string;
  description?: string;
  status: "draft" | "active" | "archived";
  rules: PricingRule[];
  activated_at?: string;
  created_at: string;
  updated_at: string;
}

export interface PricingResult {
  product_code: string;
  unit_price_cents: number;
  rule_id: string;
  scope_type: PricingScopeType;
  scope_id?: string;
  quantity: number;
  subtotal_cents: number;
}

export interface InvoiceLineItem {
  line_id: string;
  product_code: string;
  product_name: string;
  quantity: number;
  unit_price_cents: number;
  subtotal_cents: number;
  tax_cents: number;
  total_cents: number;
  order_id?: string;
}

export interface Invoice {
  invoice_id: string;
  tenant_id: string;
  account_id: string;
  customer_id: string;
  invoice_number: string;
  status: InvoiceStatus;
  line_items: InvoiceLineItem[];
  subtotal_cents: number;
  tax_cents: number;
  total_cents: number;
  amount_paid_cents: number;
  remaining_cents: number;
  due_date: string;
  issued_at?: string;
  paid_at?: string;
  voided_at?: string;
  qbo_push_state: QBOPushState;
  external_refs: Record<string, string>;
  created_at: string;
  updated_at: string;
}

export interface Payment {
  payment_id: string;
  tenant_id: string;
  invoice_id: string;
  account_id: string;
  amount_cents: number;
  source: PaymentSource;
  method: PaymentMethod;
  status: PaymentStatus;
  external_id?: string;
  reference?: string;
  applied_at?: string;
  reversed_at?: string;
  created_at: string;
}

export interface InvoiceEvent {
  event_id: string;
  tenant_id: string;
  invoice_id: string;
  event_type: InvoiceEventType;
  actor: string;
  sequence_number: number;
  occurred_at: string;
  metadata?: Record<string, unknown>;
}

export interface AccountEvent {
  event_id: string;
  tenant_id: string;
  account_id: string;
  event_type: AccountEventType;
  actor: string;
  sequence_number: number;
  occurred_at: string;
  metadata?: Record<string, unknown>;
}

export interface AgingBuckets {
  current_cents: number;
  days_1_30_cents: number;
  days_31_60_cents: number;
  days_61_90_cents: number;
  days_over_90_cents: number;
  total_cents: number;
}

// ─── Request / Response Helpers ──────────────────────────────────────────────

export interface PaginatedResponse<T> {
  data: T[];
  pagination: {
    page: number;
    size: number;
    total: number;
    total_pages: number;
  };
  request_id: string;
}

export interface SingleResponse<T> {
  data: T;
  request_id: string;
}

export interface CreateCustomerPayload {
  display_name: string;
  legal_name?: string;
  tax_id?: string;
  email?: string;
  phone?: string;
  tags?: string[];
}

export interface UpdateCustomerPayload {
  display_name?: string;
  legal_name?: string;
  tax_id?: string;
  email?: string;
  phone?: string;
  status?: CustomerStatus;
  tags?: string[];
}

export interface CreateAccountPayload {
  customer_id: string;
  display_name: string;
  tier?: AccountTier;
  credit_limit_cents?: number;
  net_terms_days?: number;
  billing_address?: BillingAddress;
}

export interface UpdateAccountPayload {
  display_name?: string;
  tier?: AccountTier;
  credit_limit_cents?: number;
  net_terms_days?: number;
  billing_address?: BillingAddress;
  status?: AccountStatus;
}

export interface CreditOverridePayload {
  reason: string;
  expires_at?: string;
}

export interface CreatePriceBookPayload {
  name: string;
  description?: string;
  rules: Omit<PricingRule, "rule_id">[];
}

export interface UpdatePriceBookPayload {
  name?: string;
  description?: string;
  rules?: Omit<PricingRule, "rule_id">[];
}

export interface PricingResolveRequest {
  account_id: string;
  product_code: string;
  quantity: number;
}

export interface VoidInvoicePayload {
  reason: string;
  force?: boolean;
}

export interface CreatePaymentPayload {
  invoice_id: string;
  amount_cents: number;
  source: PaymentSource;
  method: PaymentMethod;
  external_id?: string;
  reference?: string;
}

export interface ARAgingSummary {
  tenant_id: string;
  snapshot_date: string;
  total_accounts: number;
  accounts_with_balance: number;
  buckets: AgingBuckets;
  top_accounts: Array<{
    account_id: string;
    display_name: string;
    customer_name: string;
    total_outstanding_cents: number;
    aging: AgingBuckets;
  }>;
}

export interface ARAgingHistory {
  snapshots: Array<{
    snapshot_date: string;
    buckets: AgingBuckets;
  }>;
}
