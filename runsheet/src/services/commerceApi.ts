import { getAuthToken } from "../utils/auth";
import { API_TIMEOUTS, ApiError, ApiTimeoutError } from "./api";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── Shared Types ────────────────────────────────────────────────────────────

export interface CursorPaginatedResponse<T> {
  data: T[];
  cursor: string | null;
  has_more: boolean;
  request_id: string;
}

export interface SingleResponse<T> {
  data: T;
  request_id: string;
}

// ─── Commerce Types ──────────────────────────────────────────────────────────

// Customer types
export type CustomerStatus = "active" | "archived";

export interface Customer {
  customer_id: string;
  tenant_id: string;
  display_name: string;
  legal_name: string | null;
  primary_email: string | null;
  tax_id: string | null;
  status: CustomerStatus;
  account_count?: number;
  open_balance_cents?: number;
  lifetime_revenue_cents?: number;
  created_at: string;
  updated_at: string;
  external_refs: Record<string, string>;
  metadata: Record<string, unknown>;
}

export interface CustomerWithProjections extends Customer {
  open_invoice_count: number;
  open_balance_cents: number;
  lifetime_revenue_cents: number;
  account_count: number;
}

export interface CreateCustomerPayload {
  display_name: string;
  legal_name?: string;
  primary_email?: string;
  tax_id?: string;
  status?: CustomerStatus;
}

export interface UpdateCustomerPayload {
  display_name?: string;
  legal_name?: string;
  primary_email?: string;
  tax_id?: string;
  status?: CustomerStatus;
}

// Account types
export type AccountStatus = "active" | "suspended" | "closed";
export type CreditState = "ok" | "hold" | "override";
export type PaymentMethodPreference = "invoice" | "ach" | "card";
export type AccountTier = "platinum" | "gold" | "silver" | "bronze" | "default";

export interface BillingAddress {
  line1: string;
  line2?: string;
  city: string;
  state: string;
  zip: string;
  country: string;
}

export interface Account {
  account_id: string;
  tenant_id: string;
  customer_id: string;
  display_name: string;
  status: AccountStatus;
  credit_limit_cents: number;
  open_balance_cents: number;
  available_credit_cents: number;
  credit_balance_cents: number;
  credit_state: CreditState;
  credit_override_expires_at: string | null;
  net_terms_days: number;
  tier: AccountTier;
  billing_address: BillingAddress | null;
  payment_method_preference: PaymentMethodPreference;
  created_at: string;
  updated_at: string;
  external_refs: Record<string, string>;
}

export interface CreateAccountPayload {
  customer_id: string;
  display_name: string;
  credit_limit_cents: number;
  net_terms_days: number;
  billing_address?: BillingAddress;
  payment_method_preference?: PaymentMethodPreference;
  status?: AccountStatus;
}

export interface UpdateAccountPayload {
  display_name?: string;
  credit_limit_cents?: number;
  net_terms_days?: number;
  billing_address?: BillingAddress;
  payment_method_preference?: PaymentMethodPreference;
  status?: AccountStatus;
}

export interface CreditOverridePayload {
  reason: string;
  authorized_by: string;
  expires_at: string;
}

export interface AgingBuckets {
  bucket_0_30_cents: number;
  bucket_31_60_cents: number;
  bucket_61_90_cents: number;
  bucket_90_plus_cents: number;
  total_open_cents: number;
}

// Price Book types
export type PriceBookStatus = "draft" | "active" | "archived";
export type PricingScopeType = "account" | "tier" | "default";

export interface PricingRule {
  rule_id: string;
  price_book_id: string;
  product_code: string;
  scope_type: PricingScopeType;
  scope_value: string;
  effective_from: string;
  effective_to: string | null;
  min_quantity_gallons: number | null;
  unit_price_cents: number;
  created_at: string;
}

export interface PriceBook {
  price_book_id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  status: PriceBookStatus;
  rule_count: number;
  created_at: string;
  updated_at: string;
}

export interface CreatePriceBookPayload {
  name: string;
  description?: string;
  status?: PriceBookStatus;
  rules: Omit<PricingRule, "rule_id" | "price_book_id" | "created_at">[];
}

export interface UpdatePriceBookPayload {
  name?: string;
  description?: string;
  status?: PriceBookStatus;
  rules?: Omit<PricingRule, "rule_id" | "price_book_id" | "created_at">[];
}

export interface PricingResolveRequest {
  account_id: string;
  product_code: string;
  quantity_gallons: number;
  moment?: string;
}

export interface PricingResolveResult {
  unit_price_cents: number;
  rule_id: string;
  scope_type: PricingScopeType;
  matched_from_cache: boolean;
}

// Invoice types
export type InvoiceStatus =
  | "draft"
  | "open"
  | "partial"
  | "paid"
  | "overdue"
  | "void";

export type QboPushState = "pending" | "pushed" | "retry" | "dead_letter";

export interface InvoiceLineItem {
  line_id: string;
  product_code: string;
  quantity_gallons: number;
  unit_price_cents: number;
  subtotal_cents: number;
}

export interface Invoice {
  invoice_id: string;
  tenant_id: string;
  customer_id: string;
  account_id: string;
  order_id: string | null;
  invoice_number: string;
  status: InvoiceStatus;
  total_cents: number;
  amount_paid_cents: number;
  remaining_cents: number;
  tax_cents: number;
  subtotal_cents: number;
  line_items: InvoiceLineItem[];
  issued_at: string;
  due_date: string;
  finalized_at: string | null;
  voided_at: string | null;
  void_reason: string | null;
  qbo_push_state: QboPushState;
  qbo_push_attempts: number;
  qbo_push_last_error: string | null;
  external_refs: Record<string, string>;
  created_at: string;
  updated_at: string;
}

export type InvoiceEventType =
  | "created"
  | "finalized"
  | "payment_applied"
  | "voided"
  | "overdue_marked"
  | "payment_reversed";

export interface InvoiceEvent {
  event_id: string;
  invoice_id: string;
  tenant_id: string;
  event_type: InvoiceEventType;
  payload: Record<string, unknown>;
  occurred_at: string;
  actor: string;
}

export interface VoidInvoicePayload {
  reason: string;
  force?: boolean;
  authorized_by?: string;
}

// Payment types
export type PaymentSource =
  | "stripe"
  | "qbo"
  | "manual"
  | "account_credit"
  | "void_cascade";

export type PaymentMethod =
  | "card"
  | "ach"
  | "wire"
  | "check"
  | "credit_balance"
  | "other";

export type PaymentStatus = "applied" | "reversed";

export interface Payment {
  payment_id: string;
  tenant_id: string;
  invoice_id: string;
  account_id: string;
  amount_cents: number;
  source: PaymentSource;
  method: PaymentMethod;
  external_id: string | null;
  reference: string | null;
  status: PaymentStatus;
  received_at: string;
  applied_at: string;
  reversed_at: string | null;
}

export interface CreatePaymentPayload {
  invoice_id: string;
  amount_cents: number;
  method: PaymentMethod;
  reference?: string;
  received_at?: string;
}

// AR Aging types
export interface TenantAgingResponse extends AgingBuckets {
  by_account: Array<{
    account_id: string;
    display_name: string;
    total_open_cents: number;
    bucket_0_30_cents: number;
    bucket_31_60_cents: number;
    bucket_61_90_cents: number;
    bucket_90_plus_cents: number;
  }>;
}

export interface AgingSnapshot {
  snapshot_id: string;
  tenant_id: string;
  snapshot_date: string;
  total_open_cents: number;
  bucket_0_30_cents: number;
  bucket_31_60_cents: number;
  bucket_61_90_cents: number;
  bucket_90_plus_cents: number;
  account_count_with_balance: number;
}

// ─── Filter Types ────────────────────────────────────────────────────────────

export interface CustomerFilters {
  cursor?: string;
  limit?: number;
  page?: number;
  size?: number;
  search?: string;
  status?: CustomerStatus;
}

export interface AccountFilters {
  customer_id?: string;
  status?: AccountStatus;
  tier?: AccountTier;
  credit_state?: CreditState;
  cursor?: string;
  limit?: number;
  page?: number;
  size?: number;
}

export interface InvoiceFilters {
  status?: InvoiceStatus;
  customer_id?: string;
  account_id?: string;
  qbo_push_state?: QboPushState;
  cursor?: string;
  limit?: number;
  page?: number;
  size?: number;
}

export interface PaymentFilters {
  invoice_id?: string;
  account_id?: string;
  cursor?: string;
  limit?: number;
}

export interface AgingHistoryFilters {
  from?: string;
  to?: string;
}

// ─── HTTP Helper ─────────────────────────────────────────────────────────────

async function fetchWithTimeout(
  url: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUTS.STANDARD,
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    return response;
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new ApiTimeoutError(
        `Request timed out after ${timeout / 1000} seconds`,
      );
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

function buildQueryString(
  params: Record<string, string | number | boolean | undefined | null>,
): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== null && v !== "",
  );
  if (entries.length === 0) return "";
  const searchParams = new URLSearchParams();
  for (const [key, value] of entries) {
    searchParams.set(key, String(value));
  }
  return `?${searchParams.toString()}`;
}

async function commerceRequest<T>(
  endpoint: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
  try {
    // Get auth token if available (async)
    const token = await getAuthToken();
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options?.headers as Record<string, string> | undefined),
    };

    // Add Authorization header if token exists
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }

    const response = await fetchWithTimeout(url, {
      ...options,
      headers,
    });

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(
        body.detail || body.message || `HTTP error! status: ${response.status}`,
        response.status,
      );
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiTimeoutError || error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Unknown error",
      0,
    );
  }
}

// ─── Customer Endpoints ──────────────────────────────────────────────────────

/** POST /commerce/customers — create a new customer */
export async function createCustomer(
  payload: CreateCustomerPayload,
): Promise<SingleResponse<Customer>> {
  return commerceRequest<SingleResponse<Customer>>("/commerce/customers", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** GET /commerce/customers — paginated customer list */
export async function getCustomers(
  filters: CustomerFilters = {},
): Promise<CursorPaginatedResponse<Customer>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return commerceRequest<CursorPaginatedResponse<Customer>>(
    `/commerce/customers${qs}`,
  );
}

/** GET /commerce/customers/:id — single customer with aggregate projections */
export async function getCustomer(
  customerId: string,
): Promise<SingleResponse<CustomerWithProjections>> {
  return commerceRequest<SingleResponse<CustomerWithProjections>>(
    `/commerce/customers/${encodeURIComponent(customerId)}`,
  );
}

/** PATCH /commerce/customers/:id — update a customer */
export async function updateCustomer(
  customerId: string,
  payload: UpdateCustomerPayload,
): Promise<SingleResponse<Customer>> {
  return commerceRequest<SingleResponse<Customer>>(
    `/commerce/customers/${encodeURIComponent(customerId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

// ─── Account Endpoints ───────────────────────────────────────────────────────

/** POST /commerce/accounts — create a new account */
export async function createAccount(
  payload: CreateAccountPayload,
): Promise<SingleResponse<Account>> {
  return commerceRequest<SingleResponse<Account>>("/commerce/accounts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** GET /commerce/accounts — paginated account list */
export async function getAccounts(
  filters: AccountFilters = {},
): Promise<CursorPaginatedResponse<Account>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return commerceRequest<CursorPaginatedResponse<Account>>(
    `/commerce/accounts${qs}`,
  );
}

/** GET /commerce/accounts/:id — single account with credit details */
export async function getAccount(
  accountId: string,
): Promise<SingleResponse<Account>> {
  return commerceRequest<SingleResponse<Account>>(
    `/commerce/accounts/${encodeURIComponent(accountId)}`,
  );
}

/** PATCH /commerce/accounts/:id — update an account */
export async function updateAccount(
  accountId: string,
  payload: UpdateAccountPayload,
): Promise<SingleResponse<Account>> {
  return commerceRequest<SingleResponse<Account>>(
    `/commerce/accounts/${encodeURIComponent(accountId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

/** POST /commerce/accounts/:id/credit-override — apply a credit override */
export async function applyCreditOverride(
  accountId: string,
  payload: CreditOverridePayload,
): Promise<SingleResponse<Account>> {
  return commerceRequest<SingleResponse<Account>>(
    `/commerce/accounts/${encodeURIComponent(accountId)}/credit-override`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** DELETE /commerce/accounts/:id/credit-override — remove a credit override */
export async function deleteCreditOverride(
  accountId: string,
): Promise<SingleResponse<Account>> {
  return commerceRequest<SingleResponse<Account>>(
    `/commerce/accounts/${encodeURIComponent(accountId)}/credit-override`,
    {
      method: "DELETE",
    },
  );
}

/** GET /commerce/accounts/:id/aging — account-level AR aging buckets */
export async function getAccountAging(
  accountId: string,
): Promise<SingleResponse<AgingBuckets>> {
  return commerceRequest<SingleResponse<AgingBuckets>>(
    `/commerce/accounts/${encodeURIComponent(accountId)}/aging`,
  );
}

// ─── Price Book Endpoints ────────────────────────────────────────────────────

/** POST /commerce/price-books — create a new price book */
export async function createPriceBook(
  payload: CreatePriceBookPayload,
): Promise<SingleResponse<PriceBook>> {
  return commerceRequest<SingleResponse<PriceBook>>("/commerce/price-books", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** GET /commerce/price-books — list price books */
export async function getPriceBooks(): Promise<
  CursorPaginatedResponse<PriceBook>
> {
  return commerceRequest<CursorPaginatedResponse<PriceBook>>(
    "/commerce/price-books",
  );
}

/** GET /commerce/price-books/:id — single price book */
export async function getPriceBook(
  priceBookId: string,
): Promise<SingleResponse<PriceBook & { rules: PricingRule[] }>> {
  return commerceRequest<SingleResponse<PriceBook & { rules: PricingRule[] }>>(
    `/commerce/price-books/${encodeURIComponent(priceBookId)}`,
  );
}

/** PUT /commerce/price-books/:id — replace a price book */
export async function updatePriceBook(
  priceBookId: string,
  payload: UpdatePriceBookPayload,
): Promise<SingleResponse<PriceBook>> {
  return commerceRequest<SingleResponse<PriceBook>>(
    `/commerce/price-books/${encodeURIComponent(priceBookId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

/** POST /commerce/price-books/:id/activate — activate a price book */
export async function activatePriceBook(
  priceBookId: string,
): Promise<SingleResponse<PriceBook>> {
  return commerceRequest<SingleResponse<PriceBook>>(
    `/commerce/price-books/${encodeURIComponent(priceBookId)}/activate`,
    {
      method: "POST",
    },
  );
}

/** POST /commerce/pricing/resolve — dry-run pricing resolution */
export async function resolvePricing(
  payload: PricingResolveRequest,
): Promise<SingleResponse<PricingResolveResult>> {
  return commerceRequest<SingleResponse<PricingResolveResult>>(
    "/commerce/pricing/resolve",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

// ─── Invoice Endpoints ───────────────────────────────────────────────────────

/** GET /commerce/invoices — paginated invoice list */
export async function getInvoices(
  filters: InvoiceFilters = {},
): Promise<CursorPaginatedResponse<Invoice>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return commerceRequest<CursorPaginatedResponse<Invoice>>(
    `/commerce/invoices${qs}`,
  );
}

/** GET /commerce/invoices/:id — single invoice */
export async function getInvoice(
  invoiceId: string,
): Promise<SingleResponse<Invoice>> {
  return commerceRequest<SingleResponse<Invoice>>(
    `/commerce/invoices/${encodeURIComponent(invoiceId)}`,
  );
}

/** POST /commerce/invoices/:id/void — void an invoice */
export async function voidInvoice(
  invoiceId: string,
  payload: VoidInvoicePayload,
): Promise<SingleResponse<Invoice>> {
  return commerceRequest<SingleResponse<Invoice>>(
    `/commerce/invoices/${encodeURIComponent(invoiceId)}/void`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** GET /commerce/invoices/:id/events — invoice event timeline */
export async function getInvoiceEvents(
  invoiceId: string,
): Promise<SingleResponse<InvoiceEvent[]>> {
  return commerceRequest<SingleResponse<InvoiceEvent[]>>(
    `/commerce/invoices/${encodeURIComponent(invoiceId)}/events`,
  );
}

/** POST /commerce/invoices/:id/retry-qbo-push — retry QBO push for dead-lettered invoice */
export async function retryQboPush(
  invoiceId: string,
): Promise<SingleResponse<Invoice>> {
  return commerceRequest<SingleResponse<Invoice>>(
    `/commerce/invoices/${encodeURIComponent(invoiceId)}/retry-qbo-push`,
    {
      method: "POST",
    },
  );
}

// ─── Payment Endpoints ───────────────────────────────────────────────────────

/** POST /commerce/payments — create a manual payment */
export async function createPayment(
  payload: CreatePaymentPayload,
): Promise<SingleResponse<Payment>> {
  return commerceRequest<SingleResponse<Payment>>("/commerce/payments", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** GET /commerce/payments — paginated payment list */
export async function getPayments(
  filters: PaymentFilters = {},
): Promise<CursorPaginatedResponse<Payment>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return commerceRequest<CursorPaginatedResponse<Payment>>(
    `/commerce/payments${qs}`,
  );
}

/** GET /commerce/payments/:id — single payment */
export async function getPayment(
  paymentId: string,
): Promise<SingleResponse<Payment>> {
  return commerceRequest<SingleResponse<Payment>>(
    `/commerce/payments/${encodeURIComponent(paymentId)}`,
  );
}

/** POST /commerce/payments/:id/reverse — reverse a payment */
export async function reversePayment(
  paymentId: string,
): Promise<SingleResponse<Payment>> {
  return commerceRequest<SingleResponse<Payment>>(
    `/commerce/payments/${encodeURIComponent(paymentId)}/reverse`,
    {
      method: "POST",
    },
  );
}

// ─── AR Aging Endpoints ──────────────────────────────────────────────────────

/** GET /commerce/ar-aging — tenant-level AR aging with top accounts */
export async function getArAging(): Promise<
  SingleResponse<TenantAgingResponse>
> {
  return commerceRequest<SingleResponse<TenantAgingResponse>>(
    "/commerce/ar-aging",
  );
}

/** GET /commerce/ar-aging/history — historical aging snapshots */
export async function getArAgingHistory(
  filters: AgingHistoryFilters = {},
): Promise<SingleResponse<AgingSnapshot[]>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return commerceRequest<SingleResponse<AgingSnapshot[]>>(
    `/commerce/ar-aging/history${qs}`,
  );
}
