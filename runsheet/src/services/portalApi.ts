/**
 * Customer portal API client (customer portal design §3, §1.7).
 *
 * Every call goes through `fetchWithSession` + `apiErrorFromResponse`, like
 * `commerceApi.ts`, so the SuperTokens session cookie rides along and error
 * bodies become `ApiError`s with the envelope's `error_code`. Downloads use the
 * `exportApi.ts` blob pattern. There is deliberately no WebSocket here: the
 * portal polls where it needs fresh state (payment status).
 *
 * A 429 carries the server's `Retry-After` in `error.details.retry_after_seconds`
 * so the UI can say "Try again in N seconds" (R9.2).
 */

import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import { apiErrorFromResponse } from "./apiErrors";
import { filenameFromContentDisposition, saveBlob } from "./exportApi";
import { buildQueryString, fetchWithTimeout } from "./utils";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";

/** PDF and CSV downloads stream; bound the time to headers like exportApi. */
const DOWNLOAD_TIMEOUT_MS = 120_000;

// ─── Types (mirror Runsheet-backend/portal/models.py) ───────────────────────

export interface PortalMeasurementUnits {
  volume: string;
  distance: string;
}

export interface PortalMe {
  email: string;
  customer_display_name: string;
  supplier_name: string;
  /**
   * The tenant's IANA time zone (portal-fixes A5). Portal dates and delivery
   * windows render in it so they read like the dispatcher's. Optional for an
   * older API; the browser zone applies then.
   */
  time_zone?: string;
  /** The tenant's portal online-ordering setting (not the intake rollout flag). */
  ordering_available: boolean;
  invoices_available: boolean;
  payments_available: boolean;
  measurement_units: PortalMeasurementUnits;
  /**
   * PE4: server-side open balance over open, partial and overdue invoices
   * (null while invoicing is off). Optional so the UI keeps its client-side
   * sum against an API that predates it.
   */
  open_balance_cents?: number | null;
  open_invoice_count?: number | null;
  overdue_count?: number | null;
}

export interface PortalSingle<T> {
  data: T;
  request_id: string;
}

export interface PortalList<T> {
  data: T[];
  next_cursor: string | null;
  limit: number;
  request_id: string;
}

export interface PageParams {
  cursor?: string | null;
  limit?: number;
}

/** PE7: `active` = not delivered, not delivered-failed, not cancelled. */
export type PortalOrderStatusGroup = "active" | "past";

export interface PortalOrderFilters extends PageParams {
  status_group?: PortalOrderStatusGroup;
}

export type PortalOrderStatusCode =
  | "awaiting_confirmation"
  | "on_hold"
  | "confirmed"
  | "out_for_delivery"
  | "delivered"
  | "not_delivered"
  | "cancelled";

export interface PortalOrder {
  order_id: string;
  status_code: PortalOrderStatusCode | string;
  status_label: string;
  product_code: string | null;
  gallons_requested: number | null;
  fill_to_full: boolean;
  window_start: string | null;
  window_end: string | null;
  po_number: string | null;
  tank: { customer_tank_id: string; label: string } | null;
  created_at: string | null;
  delivered_at: string | null;
  delivered_gallons: number | null;
  ticket_number: string | null;
  cancellable: boolean;
}

export type PortalQuantity =
  | { mode: "fill_to_full" }
  | { mode: "gallons"; gallons: number };

export interface PortalOrderRequest {
  client_event_id: string;
  customer_tank_id: string;
  quantity: PortalQuantity;
  window_start: string;
  window_end: string;
  po_number?: string;
  notes?: string;
}

export interface PortalTank {
  customer_tank_id: string;
  label: string;
  /** PE2: staff-set customer-facing name and service address. */
  display_name?: string | null;
  service_address?: string | null;
  product_code: string;
  capacity_gallons: number;
  current_level_gallons: number;
  percent_full: number;
  last_reading_at: string | null;
  reading_stale: boolean;
  forecast: {
    runout_at: string;
    days_to_runout: number;
    generated_at: string;
  } | null;
  next_delivery: {
    order_id: string;
    /** Portal order status code (added with PE2; optional for older APIs). */
    status_code?: string | null;
    status_label: string;
    window_start: string | null;
    window_end: string | null;
  } | null;
}

export interface PortalTankDelivery {
  order_id: string;
  delivered_at: string | null;
  delivered_gallons: number | null;
  product_code: string | null;
  ticket_number: string | null;
}

export type PortalInvoiceStatus =
  | "open"
  | "partial"
  | "paid"
  | "overdue"
  | "void";

export interface PortalInvoicePaymentAttempt {
  payment_attempt_id: string;
  status_code: string;
  status_label: string;
  amount_cents: number;
  created_at: string | null;
}

export interface PortalInvoice {
  invoice_id: string;
  invoice_number: string | null;
  status_code: PortalInvoiceStatus | string;
  status_label: string;
  issued_at: string | null;
  due_date: string | null;
  created_at: string | null;
  account_display_name: string;
  subtotal_cents: number;
  tax_cents: number;
  total_cents: number;
  amount_paid_cents: number;
  remaining_cents: number;
  line_items: Array<{
    product_code: string | null;
    quantity_gallons: number | null;
    unit_price_cents: number | null;
    /** PE3: the unit price in dollars at its stored precision ("2.9193"). */
    unit_price_dollars?: string | null;
    subtotal_cents: number | null;
  }>;
  delivery: {
    delivered_at: string | null;
    actual_gallons: number | null;
    ticket_number: string | null;
  } | null;
  payment_attempt: PortalInvoicePaymentAttempt | null;
  payable: boolean;
}

export interface PortalInvoiceFilters extends PageParams {
  status?: PortalInvoiceStatus;
  start_date?: string;
  end_date?: string;
}

export type PortalPaymentStatus =
  | "creating"
  | "created"
  | "pending"
  | "succeeded"
  | "failed"
  | "canceled";

/** Terminal attempt states: the stored Idempotency-Key is discarded. */
export const TERMINAL_PAYMENT_STATUSES: ReadonlySet<string> = new Set([
  "succeeded",
  "failed",
  "canceled",
]);

export interface PortalPaymentCreated {
  payment_attempt_id: string;
  status_code: PortalPaymentStatus | string;
  amount_cents: number;
  client_secret: string | null;
  publishable_key: string | null;
}

export interface PortalPaymentAttempt {
  payment_attempt_id: string;
  invoice_id: string;
  status_code: PortalPaymentStatus | string;
  status_label: string;
  amount_cents: number;
  created_at: string | null;
  client_secret: string | null;
  publishable_key: string | null;
}

// Staff portal-access admin (§1.7, PD24)

export type PortalUserStatus = "invited" | "active" | "revoked";

export interface PortalUserGrant {
  grant_id: string;
  email: string;
  status: PortalUserStatus;
  created_at: string | null;
  revoked_at: string | null;
}

export interface PortalUserLink {
  password_set_link: string | null;
  link_error: boolean;
  email_sent: boolean;
}

export interface PortalUserInvite extends PortalUserLink {
  grant_id: string;
  email: string;
  status: PortalUserStatus;
  already_invited: boolean;
}

// ─── Error helpers ───────────────────────────────────────────────────────────

/** Seconds from a `Retry-After` header (delta-seconds or HTTP date). */
export function parseRetryAfter(
  header: string | null,
  now: number = Date.now(),
): number | null {
  if (!header) return null;
  const trimmed = header.trim();
  if (/^\d+$/.test(trimmed)) return Number.parseInt(trimmed, 10);
  const at = Date.parse(trimmed);
  if (Number.isNaN(at)) return null;
  return Math.max(0, Math.ceil((at - now) / 1000));
}

/** The 429 wait attached by this client, or `null`. */
export function retryAfterSeconds(error: unknown): number | null {
  if (!(error instanceof ApiError) || error.status !== 429) return null;
  const value = error.details?.retry_after_seconds;
  return typeof value === "number" ? value : null;
}

/** R9.2 text for a 429, using the server's Retry-After (60 s fallback). */
export function rateLimitMessage(error: unknown): string {
  const seconds = retryAfterSeconds(error) ?? 60;
  return `Too many requests. Try again in ${seconds} seconds.`;
}

export function isRateLimited(error: unknown): boolean {
  return error instanceof ApiError && error.status === 429;
}

/** True when `error` is an ApiError carrying `code`. */
export function hasErrorCode(error: unknown, code: string): boolean {
  return error instanceof ApiError && error.code === code;
}

// ─── HTTP ────────────────────────────────────────────────────────────────────

async function portalFetch(
  endpoint: string,
  init: RequestInit = {},
  timeout?: number,
): Promise<Response> {
  const url = `${API_BASE_URL}${endpoint}`;
  try {
    const headers: Record<string, string> = {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...(init.headers as Record<string, string> | undefined),
    };
    const response = await fetchWithSession(
      fetchWithTimeout,
      url,
      { ...init, headers },
      timeout,
    );
    if (!response.ok) {
      const error = await apiErrorFromResponse(response);
      if (response.status === 429) {
        const wait = parseRetryAfter(response.headers.get("Retry-After"));
        error.details = {
          ...(error.details ?? {}),
          ...(wait === null ? {} : { retry_after_seconds: wait }),
        };
      }
      throw error;
    }
    return response;
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

async function portalJson<T>(
  endpoint: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await portalFetch(endpoint, init);
  return (await response.json()) as T;
}

async function download(
  endpoint: string,
  accept: string,
  fallbackName: string,
): Promise<{ filename: string }> {
  const response = await portalFetch(
    endpoint,
    { method: "GET", headers: { Accept: accept } },
    DOWNLOAD_TIMEOUT_MS,
  );
  try {
    const blob = await response.blob();
    const filename = filenameFromContentDisposition(
      response.headers.get("Content-Disposition"),
      fallbackName,
    );
    saveBlob(blob, filename);
    return { filename };
  } catch (error) {
    throw new ApiError(
      error instanceof Error ? error.message : "Download failed",
      0,
    );
  }
}

const enc = encodeURIComponent;

// ─── Portal routes (§3) ──────────────────────────────────────────────────────

/** GET /api/portal/me — account overview and capability flags. */
export function getPortalMe(): Promise<PortalSingle<PortalMe>> {
  return portalJson("/portal/me");
}

/** GET /api/portal/orders — the customer's orders, newest first. */
export function listPortalOrders(
  params: PortalOrderFilters = {},
): Promise<PortalList<PortalOrder>> {
  return portalJson(`/portal/orders${buildQueryString(params)}`);
}

/** GET /api/portal/orders/{id}. */
export function getPortalOrder(
  orderId: string,
): Promise<PortalSingle<PortalOrder>> {
  return portalJson(`/portal/orders/${enc(orderId)}`);
}

/**
 * POST /api/portal/orders — request a delivery. 201 for a new request, 200
 * for a replay of the same `client_event_id`; both carry the order.
 */
export function createPortalOrder(
  body: PortalOrderRequest,
): Promise<PortalSingle<PortalOrder>> {
  return portalJson("/portal/orders", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** POST /api/portal/orders/{id}/cancel — only while awaiting confirmation. */
export function cancelPortalOrder(
  orderId: string,
): Promise<PortalSingle<PortalOrder>> {
  return portalJson(`/portal/orders/${enc(orderId)}/cancel`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

/** GET /api/portal/invoices — non-draft invoices, newest first. */
export function listPortalInvoices(
  filters: PortalInvoiceFilters = {},
): Promise<PortalList<PortalInvoice>> {
  return portalJson(`/portal/invoices${buildQueryString(filters)}`);
}

/** GET /api/portal/invoices/{id} — detail plus the latest payment attempt. */
export function getPortalInvoice(
  invoiceId: string,
): Promise<PortalSingle<PortalInvoice>> {
  return portalJson(`/portal/invoices/${enc(invoiceId)}`);
}

/** GET /api/portal/invoices/{id}/pdf — saved as an attachment. */
export function downloadPortalInvoicePdf(
  invoiceId: string,
): Promise<{ filename: string }> {
  return download(
    `/portal/invoices/${enc(invoiceId)}/pdf`,
    "application/pdf",
    `invoice_${invoiceId}.pdf`,
  );
}

/** GET /api/portal/invoices/export — CSV with the list's filters. */
export function downloadPortalInvoicesCsv(
  filters: Omit<PortalInvoiceFilters, "cursor" | "limit"> = {},
): Promise<{ filename: string }> {
  return download(
    `/portal/invoices/export${buildQueryString(filters)}`,
    "text/csv",
    "portal_invoices_export.csv",
  );
}

export interface PaymentCreateResult {
  /** 201 for a new attempt, 200 for a replay. */
  httpStatus: number;
  data: PortalPaymentCreated;
  /** Set when the server asks the client to poll (a `creating` replay). */
  retryAfterSeconds: number | null;
}

/**
 * POST /api/portal/invoices/{id}/payments — start (or replay) an ACH payment.
 * `idempotencyKey` must be reused for retries of the same payment.
 */
export async function createPortalPayment(
  invoiceId: string,
  idempotencyKey: string,
  amountCents?: number,
): Promise<PaymentCreateResult> {
  const response = await portalFetch(
    `/portal/invoices/${enc(invoiceId)}/payments`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(
        amountCents === undefined ? {} : { amount_cents: amountCents },
      ),
    },
  );
  const body = (await response.json()) as PortalSingle<PortalPaymentCreated>;
  return {
    httpStatus: response.status,
    data: body.data,
    retryAfterSeconds: parseRetryAfter(response.headers.get("Retry-After")),
  };
}

/**
 * GET /api/portal/payment-attempts/{id}. Polling omits the client secret;
 * pass `includeClientSecret` once, on reload, before mounting the Element.
 */
export function getPortalPaymentAttempt(
  paymentAttemptId: string,
  options: { includeClientSecret?: boolean } = {},
): Promise<PortalSingle<PortalPaymentAttempt>> {
  const qs = options.includeClientSecret ? "?include_client_secret=true" : "";
  return portalJson(`/portal/payment-attempts/${enc(paymentAttemptId)}${qs}`);
}

/** GET /api/portal/tanks — active tanks with forecast and next delivery. */
export function listPortalTanks(): Promise<PortalList<PortalTank>> {
  return portalJson("/portal/tanks");
}

/** GET /api/portal/tanks/{id}. */
export function getPortalTank(
  tankId: string,
): Promise<PortalSingle<PortalTank>> {
  return portalJson(`/portal/tanks/${enc(tankId)}`);
}

/** GET /api/portal/tanks/{id}/deliveries — the last 24 months. */
export function listPortalTankDeliveries(
  tankId: string,
  params: PageParams = {},
): Promise<PortalList<PortalTankDelivery>> {
  return portalJson(
    `/portal/tanks/${enc(tankId)}/deliveries${buildQueryString(params)}`,
  );
}

// ─── Staff portal-access admin (§1.7, admin only) ───────────────────────────

const portalUsersPath = (customerId: string) =>
  `/commerce/customers/${enc(customerId)}/portal-users`;

/** GET portal-users — invited, active and revoked users, newest first. */
export function listPortalUsers(
  customerId: string,
): Promise<{ data: PortalUserGrant[] }> {
  return portalJson(portalUsersPath(customerId));
}

/** POST portal-users — invite by email (200 + already_invited on repeat). */
export function invitePortalUser(
  customerId: string,
  email: string,
): Promise<PortalUserInvite> {
  return portalJson(portalUsersPath(customerId), {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

/** POST portal-users/{grant}/resend — mint a fresh password-set link. */
export function resendPortalUserLink(
  customerId: string,
  grantId: string,
): Promise<PortalUserLink> {
  return portalJson(`${portalUsersPath(customerId)}/${enc(grantId)}/resend`, {
    method: "POST",
  });
}

/** DELETE portal-users/{grant} — revoke access and remove the identity. */
export function revokePortalUser(
  customerId: string,
  grantId: string,
): Promise<{ grant_id: string; status: "revoked" }> {
  return portalJson(`${portalUsersPath(customerId)}/${enc(grantId)}`, {
    method: "DELETE",
  });
}

// ─── Tenant portal settings (portal-fixes B2) ───────────────────────────────

export interface PortalSettings {
  /** Customers can request deliveries in the portal (default on). */
  ordering_enabled: boolean;
}

/** GET /api/commerce/portal-settings — any staff role. */
export function getPortalSettings(): Promise<PortalSettings> {
  return portalJson("/commerce/portal-settings");
}

/** PUT /api/commerce/portal-settings — admin only. */
export function updatePortalSettings(
  settings: PortalSettings,
): Promise<PortalSettings> {
  return portalJson("/commerce/portal-settings", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}
