/**
 * Typed HTTP client for the Order Intake Pipeline REST surface.
 *
 * Mirrors the backend contract defined in
 * :mod:`Runsheet-backend/fuel/api/order_endpoints.py` for the Orders
 * page, Order Detail page, and Create Order modal. Follows the same
 * pattern as {@link fuelApi.ts} — local `ordersRequest` helper with
 * timeout + typed generics, no runtime fetch changes.
 *
 * Validates: Requirements 2.4, 2.5.
 */

import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import { buildQueryString, fetchWithTimeout } from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── Shared Types ────────────────────────────────────────────────────────────

export type OrderStatus =
  | "placed"
  | "confirmed"
  | "scheduled"
  | "dispatched"
  | "in_transit"
  | "delivered"
  | "failed"
  | "cancelled"
  | "on_hold";

export type CallType = "will_call" | "auto_fill" | "keep_full" | "one_off";

export type IntakeChannelType =
  | "voice"
  | "web_portal"
  | "dispatcher"
  | "csv"
  | "edi"
  | "api_partner"
  | "legacy";

// ─── Cross-Module Resolver Links (cross-module-entity-linkage Req 1.x/5.4) ────

/**
 * A single resolved reference as returned in an order resolver read's
 * ``links`` object. Mirrors the backend ``RefResolver``/``ResolvedRef``
 * contract (and the identical {@link schedulingApi.ResolvedLink} shape used by
 * the job resolver read):
 *
 * - ``resolved``  — the id resolved to a same-tenant entity; ``summary`` holds
 *   a small display payload (e.g. ``customer_id`` + ``display_name``). The
 *   resolved name is the source of truth for display (Req 1.4).
 * - ``unresolved`` — an id was present but did not resolve in this tenant; the
 *   UI renders an explicit "unlinked" affordance rather than a stale name
 *   (Req 1.2).
 * - ``empty`` — no id was supplied (the reference is simply absent).
 */
export type ResolvedLink =
  | { status: "resolved"; id: string; summary: Record<string, unknown> }
  | { status: "unresolved"; id: string }
  | { status: "empty"; id?: string | null };

/**
 * The ``links`` object on an order resolver read
 * (``GET /orders/{order_id}?expand=customer,asset,driver``). Each key is
 * present only when requested via ``expand``; absent keys mean the caller did
 * not ask to expand that reference. List reads (``GET /orders``) do not carry
 * ``links`` — the Orders table links optimistically on ``customer_id`` and
 * uses the ``customer_name`` snapshot as the display fallback.
 */
export interface OrderLinks {
  customer?: ResolvedLink;
  asset?: ResolvedLink;
  driver?: ResolvedLink;
}

/** The entity references an order resolver read can expand. */
export type OrderExpand = "customer" | "asset" | "driver";

// ─── Order Types ─────────────────────────────────────────────────────────────

export interface IntakeMetadata {
  call_id?: string | null;
  recording_url?: string | null;
  transcript?: string | null;
  agent_confidence?: number | null;
  dispatcher_user_id?: string | null;
  session_id?: string | null;
  portal_session_id?: string | null;
  user_agent?: string | null;
  import_batch_id?: string | null;
  csv_row_number?: number | null;
  edi_interchange_id?: string | null;
  partner_ref?: string | null;
  legacy_shipment_id?: string | null;
}

export interface FuelOrder {
  order_id: string;
  tenant_id: string;
  customer_id: string;
  customer_name: string;
  customer_phone?: string | null;
  customer_email?: string | null;
  ship_to_address: string;
  ship_to_lat: number;
  ship_to_lon: number;
  customer_tank_id?: string | null;
  product_code?: string | null;
  gallons_requested?: number | null;
  fill_to_full: boolean;
  call_type: CallType;
  delivery_window_start?: string | null;
  delivery_window_end?: string | null;
  hold_reason?: string | null;
  po_number?: string | null;
  special_instructions?: string | null;
  intake_channel: IntakeChannelType;
  intake_channel_id: string;
  intake_metadata?: IntakeMetadata;
  status: OrderStatus;
  assigned_driver_id?: string | null;
  assigned_asset_id?: string | null;
  assigned_run_id?: string | null;
  legacy_origin_snapshot?: string | null;
  source_schema_version: string;
  trace_id: string;
  created_at: string;
  updated_at: string;
  last_event_timestamp: string;
  /**
   * Resolved cross-module references, present only on resolver reads that pass
   * ``expand`` (cross-module-entity-linkage Req 5.1/5.4). Absent on plain list
   * reads. When present, the resolved customer ``summary`` name is preferred
   * over the {@link FuelOrder.customer_name} snapshot for display (Req 1.4).
   */
  links?: OrderLinks;
}

export interface FuelOrderEvent {
  event_id: string;
  order_id: string;
  tenant_id: string;
  event_type: string;
  event_payload: Record<string, unknown>;
  event_timestamp: string;
  ingested_at: string;
  source_schema_version: string;
  trace_id: string;
  location?: { lat: number; lon: number } | null;
}

// ─── Request / Filter Types ──────────────────────────────────────────────────

export interface OrderListFilters {
  status?: OrderStatus;
  customer_id?: string;
  driver_id?: string;
  call_type?: CallType;
  product_code?: string;
  start_date?: string;
  end_date?: string;
  intake_channel?: IntakeChannelType;
  /** Free-text search over order id, customer name/id, and ship-to address. */
  q?: string;
  page?: number;
  size?: number;
  sort?: string;
}

export interface CreateOrderPayload {
  customer_id: string;
  customer_name: string;
  customer_phone?: string;
  customer_email?: string;
  ship_to_address: string;
  ship_to_lat: number;
  ship_to_lon: number;
  customer_tank_id?: string;
  product_code: string;
  gallons_requested?: number;
  fill_to_full?: boolean;
  call_type: CallType;
  delivery_window_start?: string;
  delivery_window_end?: string;
  po_number?: string;
  special_instructions?: string;
  client_event_id: string;
}

export interface UpdateOrderStatusPayload {
  new_status: OrderStatus;
  reason?: string;
  notes?: string;
}

export interface AssignDriverPayload {
  driver_id: string;
}

export interface CancelOrderPayload {
  reason: string;
}

export interface HoldOrderPayload {
  hold_reason: string;
}

export interface ReleaseHoldPayload {
  notes?: string;
}

// ─── Response Types ──────────────────────────────────────────────────────────

/**
 * Response for ``GET /api/orders`` — the order endpoints return a bare
 * ``{ items, total, page, size }`` envelope (see ``OrderListResponse`` in
 * :mod:`Runsheet-backend/fuel/api/order_endpoints.py`), NOT the
 * ``{ data, pagination, request_id }`` shape used by the other list surfaces.
 * The backend model declares ``extra="forbid"``, so no additional keys are
 * emitted.
 */
export interface OrderListResponse {
  items: FuelOrder[];
  total: number;
  page: number;
  size: number;
}

/**
 * Response for ``GET /api/orders/{order_id}/events`` — a bare
 * ``{ items, total }`` envelope (``OrderEventsListResponse`` server-side),
 * with no ``data``/``request_id`` wrapper.
 */
export interface OrderEventsListResponse {
  items: FuelOrderEvent[];
  total: number;
}

/**
 * Response for ``POST /api/orders`` — the create endpoint runs the intake
 * pipeline and returns an intake result (``IntakeResultResponse`` server-side),
 * NOT a full order. ``order_id`` is present once the order is materialized;
 * ``status`` is the pipeline outcome (e.g. ``"accepted"``, ``"duplicate"``).
 */
export interface IntakeResultResponse {
  event_id: string;
  status: string;
  order_id?: string | null;
}

// ─── HTTP Helpers ────────────────────────────────────────────────────────────

async function ordersRequest<T>(
  endpoint: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
  try {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options?.headers as Record<string, string> | undefined),
    };

    // Session cookie + anti-CSRF token are attached by the SuperTokens SDK;
    // an auth failure triggers a refresh-then-retry, else a redirect to
    // sign-in (Req 8.4, 8.5).
    const response = await fetchWithSession(fetchWithTimeout, url, {
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

// ─── Order Endpoints ─────────────────────────────────────────────────────────

/** GET /api/orders — list orders with filters and pagination */
export async function listOrders(
  filters: OrderListFilters = {},
): Promise<OrderListResponse> {
  const qs = buildQueryString(filters);
  return ordersRequest<OrderListResponse>(`/orders${qs}`);
}

/**
 * GET /api/orders/:order_id — fetch a single order by ID.
 *
 * Pass ``options.expand`` (cross-module-entity-linkage Req 1.1/5.1/5.4) to
 * resolve cross-module references into the order's ``links`` object — each
 * either a resolved summary or an explicit ``unresolved``/``empty`` marker so
 * the UI can render an "unlinked" affordance. Omitting ``expand`` keeps the
 * pre-existing, additive-only order contract unchanged (Req 6.3).
 */
export async function getOrder(
  orderId: string,
  options?: { expand?: OrderExpand[] },
): Promise<FuelOrder> {
  const qs = options?.expand?.length
    ? `?expand=${options.expand.join(",")}`
    : "";
  return ordersRequest<FuelOrder>(
    `/orders/${encodeURIComponent(orderId)}${qs}`,
  );
}

/** GET /api/orders/:order_id/events — fetch the event timeline for an order */
export async function getOrderEvents(
  orderId: string,
): Promise<OrderEventsListResponse> {
  return ordersRequest<OrderEventsListResponse>(
    `/orders/${encodeURIComponent(orderId)}/events`,
  );
}

/**
 * POST /api/orders — create a new fuel order (dispatcher keyboard).
 *
 * Returns the intake-pipeline result ({@link IntakeResultResponse}), not a
 * full order. The new order's id is available on ``order_id`` once the order
 * is materialized.
 */
export async function createOrder(
  payload: CreateOrderPayload,
): Promise<IntakeResultResponse> {
  return ordersRequest<IntakeResultResponse>("/orders", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** PATCH /api/orders/:order_id/status — transition order status */
export async function updateOrderStatus(
  orderId: string,
  payload: UpdateOrderStatusPayload,
): Promise<FuelOrder> {
  return ordersRequest<FuelOrder>(
    `/orders/${encodeURIComponent(orderId)}/status`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

/** PATCH /api/orders/:order_id/assign — assign a driver to an order */
export async function assignDriver(
  orderId: string,
  payload: AssignDriverPayload,
): Promise<FuelOrder> {
  return ordersRequest<FuelOrder>(
    `/orders/${encodeURIComponent(orderId)}/assign`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

/** POST /api/orders/:order_id/cancel — cancel an order with a reason */
export async function cancelOrder(
  orderId: string,
  payload: CancelOrderPayload,
): Promise<FuelOrder> {
  return ordersRequest<FuelOrder>(
    `/orders/${encodeURIComponent(orderId)}/cancel`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** POST /api/orders/:order_id/hold — place an order on hold with a reason */
export async function holdOrder(
  orderId: string,
  payload: HoldOrderPayload,
): Promise<FuelOrder> {
  return ordersRequest<FuelOrder>(
    `/orders/${encodeURIComponent(orderId)}/hold`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/**
 * POST /api/orders/:order_id/release-hold — release an order from hold.
 *
 * Re-runs the registered intake hooks (pricing, credit-check, etc.). If
 * all pass, the order transitions back to ``placed``; if any hook fails,
 * the order stays ``on_hold`` with an updated ``hold_reason``. The returned
 * order reflects the resulting status.
 */
export async function releaseHoldOrder(
  orderId: string,
  payload: ReleaseHoldPayload = {},
): Promise<FuelOrder> {
  return ordersRequest<FuelOrder>(
    `/orders/${encodeURIComponent(orderId)}/release-hold`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

// ─── Bulk Create ─────────────────────────────────────────────────────────────

/**
 * A single row in a bulk-upload request.
 *
 * Mirrors ``BulkOrderRow`` in
 * :mod:`Runsheet-backend/fuel/api/order_endpoints.py`. ``client_event_id``
 * is optional per-row — when omitted the backend derives an idempotency
 * key from the row contents. ``schema_version`` defaults to ``"1.0"``
 * server-side, so callers rarely set it.
 */
export interface BulkOrderRow {
  client_event_id?: string;
  customer_id: string;
  customer_name: string;
  customer_phone?: string;
  customer_email?: string;
  ship_to_address: string;
  ship_to_lat: number;
  ship_to_lon: number;
  customer_tank_id?: string;
  product_code: string;
  gallons_requested?: number;
  fill_to_full?: boolean;
  call_type: CallType;
  delivery_window_start?: string;
  delivery_window_end?: string;
  po_number?: string;
  special_instructions?: string;
  schema_version?: string;
}

export interface BulkOrderRequest {
  orders: BulkOrderRow[];
  /** When true, validates every row without persisting any order. */
  dry_run?: boolean;
}

export interface BulkOrderResultItem {
  row_index: number;
  order_id?: string | null;
  event_id?: string | null;
  /** Per-row outcome: e.g. ``created``, ``duplicate``, ``error``, ``valid``. */
  status: string;
  error?: string | null;
}

export interface BulkOrderResponse {
  total: number;
  processed: number;
  duplicates: number;
  errors: number;
  dry_run: boolean;
  results: BulkOrderResultItem[];
}

/**
 * POST /api/orders/bulk — bulk-create fuel orders (up to 1000 rows).
 *
 * Pass ``dry_run: true`` to validate all rows without persisting; the
 * response ``results`` array still reports per-row status so a dispatcher
 * can preview a CSV import before committing. Exceeding the 1000-row cap
 * is rejected by the backend with a 400. Unlike the other endpoints this
 * route returns the ``BulkOrderResponse`` payload directly (no
 * ``{ data, request_id }`` envelope).
 */
export async function createOrdersBulk(
  payload: BulkOrderRequest,
): Promise<BulkOrderResponse> {
  return ordersRequest<BulkOrderResponse>("/orders/bulk", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
