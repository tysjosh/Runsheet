/**
 * Margin feed admin API client (`/api/commerce/margin`, tenant admin only).
 *
 * Every response is `{ data, request_id }`; these helpers return `data`.
 * Money fields are integer cents or micros, and a missing cost is `null`
 * (never 0). Format them with `components/commerce/margin/marginFormat`.
 */
import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import { apiErrorFromResponse } from "./apiErrors";
import { buildQueryString, fetchWithTimeout } from "./utils";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";
const BASE = "/commerce/margin";

// ─── Types ───────────────────────────────────────────────────────────────────

export type MarginStage = "order_estimate" | "delivery" | "invoice";
export type MarginFlag =
  | "missing_cost"
  | "negative_margin"
  | "below_floor"
  | "terminal_unattributed";
export type CostMethod = "override" | "wac" | "rack_fallback" | "none";
export type RecordStatus = "active" | "superseded" | "void";

export interface MarginRecord {
  record_id: string;
  stage: MarginStage;
  source_key: string;
  order_id: string | null;
  invoice_id: string | null;
  line_index: number | null;
  customer_id: string | null;
  account_id: string | null;
  product_code: string;
  terminal_id: string | null;
  gallons_ugal: number;
  unit_price_micros: number;
  revenue_cents: number;
  method: CostMethod;
  product_cost_micros: number | null;
  adders_micros: number | null;
  landed_cost_micros: number | null;
  cost_cents: number | null;
  margin_cents: number | null;
  margin_per_gallon_micros: number | null;
  margin_bp: number | null;
  margin_pct: string | null;
  no_cost_reason: string | null;
  flags: MarginFlag[];
  floor_micros_used: number;
  cost_snapshot: Record<string, unknown>;
  as_of: string;
  version: number;
  status: RecordStatus;
  origin: "live" | "recompute";
  frozen_at: string | null;
  computed_at: string;
}

export interface MarginRecordDetail extends MarginRecord {
  versions: MarginRecord[];
}

export interface MarginPage<T> {
  items: T[];
  next_cursor: string | null;
  total?: number | null;
}

/** `GET /records` page plus the settings timezone its dates use. */
export interface MarginRecordPage extends MarginPage<MarginRecord> {
  timezone: string;
}

export interface MarginRecordFilters {
  start_date?: string;
  end_date?: string;
  customer_id?: string;
  product_code?: string;
  terminal_id?: string;
  stage?: MarginStage | "";
  flag?: MarginFlag | "";
  status?: RecordStatus | "all" | "";
}

export interface SummaryBlock {
  records: number;
  gallons_ugal: number;
  revenue_cents: number;
  revenue_cents_with_cost: number;
  revenue_cents_missing_cost: number;
  cost_cents: number;
  margin_cents: number;
  margin_bp: number | null;
  margin_pct: string | null;
  flag_counts: Record<string, { records: number; gallons_ugal: number }>;
  missing_cost_share_bp: number | null;
}

export interface MarginSummary {
  group_by: "day" | "customer" | "product" | "terminal";
  start_date: string;
  end_date: string;
  timezone: string;
  groups: (SummaryBlock & { key: string })[];
  totals: SummaryBlock;
  skipped_sources: { count: number; sample: Record<string, unknown>[] };
}

export type AlertStatus =
  | "open"
  | "acknowledged"
  | "pending_review"
  | "approved"
  | "dismissed";

export interface MarginAlert {
  alert_id: string;
  alert_type:
    | "negative_margin"
    | "missing_cost"
    | "leakage_proposal"
    | "recompute_digest";
  severity: "high" | "medium" | "info";
  status: AlertStatus;
  record_id: string | null;
  order_id: string | null;
  run_id: string | null;
  proposal_id: string | null;
  customer_id: string | null;
  product_code: string | null;
  details: Record<string, unknown>;
  created_at: string;
  resolved_by: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
}

export type CostEntryKind = "purchase" | "override" | "adder";

export interface CostEntry {
  entry_id: string;
  kind: CostEntryKind;
  product_code: string;
  terminal_id: string | null;
  supplier_name: string | null;
  effective_at: string;
  effective_to: string | null;
  unit_cost_micros: number;
  gallons_milli: number | null;
  adder_type: "freight" | "fee" | "other" | null;
  bol_id: string | null;
  reference: string | null;
  notes: string | null;
  status: "active" | "superseded" | "voided";
  source: "manual" | "csv_import";
  created_by: string;
  created_at: string;
}

/** Money is a decimal string (USD), never a JS number, so nothing is rounded. */
export interface CostEntryPayload {
  kind: CostEntryKind;
  product_code: string;
  terminal_id?: string;
  supplier_name?: string;
  effective_at: string;
  effective_to?: string;
  unit_cost_usd: string;
  gallons?: string;
  adder_type?: "freight" | "fee" | "other";
  bol_id?: string;
  reference?: string;
  notes?: string;
}

export interface CostEntryFilters {
  kind?: CostEntryKind | "";
  product_code?: string;
  terminal_id?: string;
  status?: "active" | "superseded" | "voided" | "all";
  cursor?: string;
  limit?: number;
}

export interface ImportRowError {
  row?: number;
  loc: (string | number)[];
  msg: string;
  type: string;
}

export interface ImportReport {
  dry_run: boolean;
  rows_total: number;
  rows_valid: number;
  duplicates: {
    row: number;
    natural_key: string;
    existing_entry_id?: string;
    duplicate_of_row?: number;
  }[];
  created_entry_ids: string[];
  warnings: string[];
  import_batch_id?: string;
}

export interface CostBasis {
  method: CostMethod;
  product_code: string;
  terminal_id: string | null;
  product_cost_micros: number | null;
  adders_micros: number | null;
  adders: Record<string, unknown>[];
  adders_configured: boolean;
  landed_cost_micros: number | null;
  override_entry_id: string | null;
  lots: {
    lot_type: string;
    id: string;
    gallons_milli: number;
    unit_cost_micros: number;
    priced_by: string;
    price_ref_id: string | null;
  }[];
  rack_price_id: string | null;
  rack_selection: string | null;
  contract_ids: string[];
  wac_window_days: number;
  rack_staleness_days: number;
  window_start: string | null;
  as_of: string;
  terminal_unattributed: boolean;
  no_cost_reason: string | null;
  diagnostics: {
    excluded: Record<string, number>;
    bols_scanned: number;
    lot_cap_exceeded: boolean;
    rack_cap_exceeded: boolean;
    zero_price_ignored: number;
  };
}

export interface MarginSettings {
  wac_window_days: number;
  rack_staleness_days: number;
  floor_micros: number;
  product_floors: Record<string, number>;
  timezone: string;
  feed_activated_at: string | null;
  updated_by: string | null;
  updated_at: string | null;
  persisted: boolean;
}

export interface MarginSettingsPayload {
  wac_window_days: number;
  rack_staleness_days: number;
  floor_usd_per_gallon: string;
  product_floors: Record<string, string>;
  timezone: string;
}

export interface RecomputePayload {
  start_date: string;
  end_date: string;
  stages: ("invoice" | "delivery")[];
  only_missing: boolean;
  reason: string;
}

export interface RecomputeRun {
  run_id: string;
  status: "running" | "completed" | "failed";
  start_date: string;
  end_date: string;
  counts: Record<string, unknown> | null;
  [key: string]: unknown;
}

// ─── HTTP helper ─────────────────────────────────────────────────────────────

async function marginRequest<T>(
  endpoint: string,
  options: RequestInit = {},
): Promise<T> {
  const url = `${API_BASE_URL}${BASE}${endpoint}`;
  const isForm =
    typeof FormData !== "undefined" && options.body instanceof FormData;
  const headers: Record<string, string> = {
    // A multipart body sets its own boundary header.
    ...(isForm ? {} : { "Content-Type": "application/json" }),
    ...(options.headers as Record<string, string> | undefined),
  };
  try {
    const response = await fetchWithSession(fetchWithTimeout, url, {
      ...options,
      headers,
    });
    if (!response.ok) {
      throw await apiErrorFromResponse(response);
    }
    const body = (await response.json()) as { data: T };
    return body.data;
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

type Params = Record<string, string | number | boolean | undefined>;

/** Drop empty strings so an unset filter is not sent as `?stage=`. */
function compact(params: object): Params {
  const out: Params = {};
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    out[key] = value as string | number | boolean;
  }
  return out;
}

/** Records filters -> query params (shared by the list and the CSV export). */
export function recordFilterParams(filters: MarginRecordFilters): Params {
  return compact(filters);
}

// ─── Records, summary, alerts ────────────────────────────────────────────────

export function getMarginRecords(
  filters: MarginRecordFilters,
  cursor?: string | null,
  limit = 50,
): Promise<MarginRecordPage> {
  const qs = buildQueryString({
    ...recordFilterParams(filters),
    ...(cursor ? { cursor } : {}),
    limit,
  });
  return marginRequest(`/records${qs}`);
}

export function getMarginRecord(recordId: string): Promise<MarginRecordDetail> {
  return marginRequest(`/records/${encodeURIComponent(recordId)}`);
}

export function getMarginSummary(params: {
  group_by: MarginSummary["group_by"];
  start_date?: string;
  end_date?: string;
}): Promise<MarginSummary> {
  return marginRequest(`/summary${buildQueryString(compact(params))}`);
}

export function getMarginAlerts(
  params: {
    status?: AlertStatus[];
    alert_type?: MarginAlert["alert_type"];
    cursor?: string | null;
    limit?: number;
  } = {},
): Promise<MarginPage<MarginAlert>> {
  const qs = buildQueryString(
    compact({
      status: params.status?.join(","),
      alert_type: params.alert_type,
      cursor: params.cursor ?? undefined,
      limit: params.limit,
    }),
  );
  return marginRequest(`/alerts${qs}`);
}

/** Number of alerts still waiting on an admin (open or pending review). */
export async function getOpenMarginAlertCount(): Promise<number> {
  const page = await getMarginAlerts({
    status: ["open", "pending_review"],
    limit: 1,
  });
  return page.total ?? page.items.length;
}

export function resolveMarginAlert(
  alertId: string,
  action: "acknowledge" | "approve" | "dismiss",
  note?: string,
): Promise<MarginAlert> {
  return marginRequest(`/alerts/${encodeURIComponent(alertId)}/${action}`, {
    method: "POST",
    body: JSON.stringify(note ? { note } : {}),
  });
}

// ─── Cost entries and cost basis ─────────────────────────────────────────────

export function getCostEntries(
  filters: CostEntryFilters = {},
): Promise<MarginPage<CostEntry>> {
  return marginRequest(`/cost-entries${buildQueryString(compact(filters))}`);
}

export function createCostEntry(
  payload: CostEntryPayload,
): Promise<{ entry: CostEntry; warnings: string[] }> {
  return marginRequest("/cost-entries", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function supersedeCostEntry(
  entryId: string,
  payload: CostEntryPayload & { reason: string },
): Promise<{ entry: CostEntry; superseded: CostEntry; warnings: string[] }> {
  return marginRequest(
    `/cost-entries/${encodeURIComponent(entryId)}/supersede`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function voidCostEntry(
  entryId: string,
  reason: string,
): Promise<{ entry: CostEntry }> {
  return marginRequest(`/cost-entries/${encodeURIComponent(entryId)}/void`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function importCostEntries(
  file: File,
  dryRun: boolean,
): Promise<ImportReport> {
  const form = new FormData();
  form.append("file", file);
  form.append("dry_run", dryRun ? "true" : "false");
  return marginRequest("/cost-entries/import", { method: "POST", body: form });
}

export function getCostBasis(params: {
  product_code: string;
  terminal_id?: string;
  as_of?: string;
}): Promise<CostBasis> {
  return marginRequest(`/cost-basis${buildQueryString(compact(params))}`);
}

// ─── Settings and recompute ──────────────────────────────────────────────────

export function getMarginSettings(): Promise<MarginSettings> {
  return marginRequest("/settings");
}

/**
 * Whether the margin feed is usable for this caller.
 *
 * The backend has no capability flag the UI can read, so this probes
 * `GET /settings` (cheap, admin-only). The margin routes answer **404
 * `COMMERCE_DISABLED`** while `COMMERCE_MARGIN_FEED_ENABLED` (or the commerce
 * backbone) is off, *before* any role check, and **403** to non-admins when
 * it is on. Only that exact 404 code counts as "disabled"; any other 404,
 * 5xx or network failure is "unknown" so the pages show their own errors.
 */
export type MarginAvailability =
  | "enabled"
  | "disabled"
  | "forbidden"
  | "unknown";

export const MARGIN_DISABLED_CODE = "COMMERCE_DISABLED";

export function isMarginDisabledError(error: unknown): boolean {
  const e = error as { status?: unknown; code?: unknown } | null;
  return e?.status === 404 && e?.code === MARGIN_DISABLED_CODE;
}

export async function getMarginAvailability(): Promise<MarginAvailability> {
  try {
    await getMarginSettings();
    return "enabled";
  } catch (error) {
    if (isMarginDisabledError(error)) return "disabled";
    if ((error as { status?: unknown })?.status === 403) return "forbidden";
    return "unknown";
  }
}

export function updateMarginSettings(
  payload: MarginSettingsPayload,
): Promise<{ settings: MarginSettings; warnings: string[] }> {
  return marginRequest("/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function startMarginRecompute(
  payload: RecomputePayload,
): Promise<{ run_id: string }> {
  return marginRequest("/recompute", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getMarginRecomputeRun(runId: string): Promise<RecomputeRun> {
  return marginRequest(`/recompute/${encodeURIComponent(runId)}`);
}
