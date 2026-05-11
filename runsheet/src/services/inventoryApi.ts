import { getAuthToken } from "../utils/auth";
import { API_TIMEOUTS, ApiError, ApiTimeoutError } from "./api";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── Shared Types ────────────────────────────────────────────────────────────

export interface PaginationMeta {
  page: number;
  size: number;
  total: number;
  total_pages: number;
}

export interface PaginatedResponse<T> {
  data: T[];
  pagination: PaginationMeta;
  request_id: string;
}

// ─── Inventory Types ─────────────────────────────────────────────────────────

export type InventoryCategory =
  | "tires"
  | "engine_parts"
  | "brake_parts"
  | "fluids"
  | "filters"
  | "electrical"
  | "fuel_equipment"
  | "safety"
  | "general";

export type InventoryStatus =
  | "in_stock"
  | "low_stock"
  | "out_of_stock"
  | "on_order";

export interface InventoryItem {
  item_id: string;
  name: string;
  category: InventoryCategory;
  quantity: number;
  unit: string;
  min_threshold: number;
  max_capacity: number;
  location: string;
  status: InventoryStatus;
  unit_cost: number | null;
  supplier: string | null;
  compatible_assets: string[] | null;
  last_restocked: string | null;
  tenant_id: string;
}

export interface InventorySummary {
  total_items: number;
  total_value: number;
  in_stock: number;
  low_stock: number;
  out_of_stock: number;
  on_order: number;
  categories: Record<string, number>;
}

export interface StockAdjustment {
  quantity_change: number;
  reason: string;
  reference_id?: string;
  notes?: string;
}

export interface StockAdjustmentResult {
  item_id: string;
  previous_quantity: number;
  new_quantity: number;
  previous_status: InventoryStatus;
  new_status: InventoryStatus;
  event_id: string;
}

export interface PartAvailability {
  item_id: string;
  name: string;
  category: string;
  status: InventoryStatus;
  quantity: number;
  min_threshold: number;
  location: string;
}

export type ReadinessStatus = "ready" | "warning" | "critical" | "blocked";

export interface AssetReadinessIndicator {
  asset_id: string;
  status: ReadinessStatus;
  missing_parts: PartAvailability[];
  low_parts: PartAvailability[];
}

// ─── Filter Types ────────────────────────────────────────────────────────────

export interface InventoryFilters {
  category?: InventoryCategory;
  status?: InventoryStatus;
  location?: string;
  page?: number;
  size?: number;
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

async function inventoryRequest<T>(
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

// ─── Inventory Endpoints ─────────────────────────────────────────────────────

/** GET /inventory/items — paginated inventory items with optional filters */
export async function getItems(
  filters: InventoryFilters = {},
): Promise<PaginatedResponse<InventoryItem>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return inventoryRequest<PaginatedResponse<InventoryItem>>(
    `/inventory/items${qs}`,
  );
}

/** GET /inventory/alerts — items below low-stock threshold or out of stock */
export async function getAlerts(): Promise<{
  data: InventoryItem[];
  count: number;
  request_id: string;
}> {
  return inventoryRequest<{
    data: InventoryItem[];
    count: number;
    request_id: string;
  }>("/inventory/alerts");
}

/** GET /inventory/summary — aggregated inventory summary */
export async function getSummary(): Promise<{
  data: InventorySummary;
  request_id: string;
}> {
  return inventoryRequest<{ data: InventorySummary; request_id: string }>(
    "/inventory/summary",
  );
}

/** POST /inventory/items/:itemId/adjust — record a stock adjustment */
export async function adjustStock(
  itemId: string,
  adjustment: StockAdjustment,
): Promise<{ data: StockAdjustmentResult; request_id: string }> {
  return inventoryRequest<{ data: StockAdjustmentResult; request_id: string }>(
    `/inventory/items/${encodeURIComponent(itemId)}/adjust`,
    {
      method: "POST",
      body: JSON.stringify(adjustment),
    },
  );
}

/** GET /inventory/readiness/:assetId — asset readiness indicator */
export async function getAssetReadiness(
  assetId: string,
): Promise<{ data: AssetReadinessIndicator; request_id: string }> {
  return inventoryRequest<{
    data: AssetReadinessIndicator;
    request_id: string;
  }>(`/inventory/readiness/${encodeURIComponent(assetId)}`);
}

/** POST /inventory/items — create a new inventory item */
export interface CreateInventoryItemPayload {
  name: string;
  category: InventoryCategory;
  quantity?: number;
  unit: string;
  min_threshold: number;
  max_capacity: number;
  location: string;
  unit_cost?: number | null;
  supplier?: string | null;
  compatible_assets?: string[] | null;
}

export async function createItem(
  payload: CreateInventoryItemPayload,
): Promise<{ data: InventoryItem; request_id: string }> {
  return inventoryRequest<{ data: InventoryItem; request_id: string }>(
    "/inventory/items",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** DELETE /inventory/items/:itemId — delete an inventory item */
export async function deleteItem(itemId: string): Promise<void> {
  await inventoryRequest<void>(
    `/inventory/items/${encodeURIComponent(itemId)}`,
    { method: "DELETE" },
  );
}

// ─── Stock Movement History ──────────────────────────────────────────────────

export interface StockMovementEvent {
  event_id: string;
  item_id: string;
  quantity_change: number;
  quantity_before: number;
  quantity_after: number;
  reason: string;
  reference_id: string | null;
  actor_id: string;
  status_before: string;
  status_after: string;
  tenant_id: string;
  event_timestamp: string;
}

/** GET /inventory/items/:itemId/history — paginated stock movement history */
export async function getItemHistory(
  itemId: string,
  page: number = 1,
  size: number = 20,
): Promise<PaginatedResponse<StockMovementEvent>> {
  const qs = buildQueryString({ page, size });
  return inventoryRequest<PaginatedResponse<StockMovementEvent>>(
    `/inventory/items/${encodeURIComponent(itemId)}/history${qs}`,
  );
}
