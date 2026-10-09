/**
 * Typed HTTP client for the Dispatch Board REST surface (`/api/fuel/board`).
 *
 * Mirrors `Runsheet-backend/fuel/services/dispatch_board_models.py` and
 * `fuel/api/dispatch_board_endpoints.py` (dispatch-board design K11). Every
 * call unwraps the `{data, request_id}` envelope. Failures become a
 * `BoardApiError`, an `ApiError` that also carries the backend's `details`
 * and `details.reason`, so the board can react to `BOARD_LANE_CONFLICT`,
 * `BOARD_COMMAND_BLOCKED` and friends without parsing messages.
 *
 * `validateBoard`, `previewPublish` and `getBoard` accept an `AbortSignal` so
 * the board can cancel superseded drag-time and dialog requests (K3.5, K7.1).
 */
import {
  API_TIMEOUTS,
  ApiError,
  ApiTimeoutError,
  fetchWithSession,
} from "./api";
import { extractApiErrorCode, extractApiErrorMessage } from "./apiErrors";
import { buildQueryString } from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";
const BOARD_BASE = "/fuel/board";

// ─── Shared enums ────────────────────────────────────────────────────────────

/** Modes `/status` can return; `disabled` is a 404 and never a body. */
export type BoardMode = "shadow" | "active_gated" | "active_auto";
export type CheckOutcome = "pass" | "warn" | "block" | "info";
export type ShiftId = "day" | "night" | "all";
export type InputModality =
  | "drag"
  | "menu"
  | "place"
  | "keyboard"
  | "suggestion"
  | "system";
export type BoardCallType = "keep_full" | "auto_fill" | "will_call" | "one_off";
export type WindowFilter = "overdue" | "today" | "later";
export type LaneState =
  | "draft"
  | "published"
  | "modified"
  | "publishing"
  | "failed"
  | "recovering";
export type CheckId =
  | "order_state"
  | "delivery_window"
  | "driver_qualification"
  | "driver_pairing"
  | "hos"
  | "asset_certification"
  | "compartment_fit"
  | "compartment_compatibility"
  | "dyed_diesel"
  | "schedule"
  | "terminal_supply"
  | "post_publish"
  | "suggestion";

// ─── Draft and lane models (K2) ──────────────────────────────────────────────

export interface LatLon {
  lat: number;
  lon: number;
}

export interface DeliveryWindow {
  start: string | null;
  end: string | null;
}

export interface OrderSnapshot {
  product_code: string | null;
  customer_id: string | null;
  customer_tank_id: string | null;
  gallons_requested: number | null;
  fill_to_full: boolean;
  window: DeliveryWindow;
  call_type: string | null;
  status: string | null;
}

export interface Stop {
  order_id: string;
  snapshot: OrderSnapshot;
  location: LatLon | null;
  eta: string | null;
}

export interface CompartmentShare {
  compartment_id: string;
  liters: number;
}

export interface Allocation {
  order_id: string | null;
  compartment_id: string;
  product_code: string | null;
  liters: number;
  capacity_liters: number;
}

export interface Load {
  load_id: string;
  shift_id: ShiftId;
  terminal_id: string | null;
  planned_start: string | null;
  planned_end: string | null;
  stops: Stop[];
  allocation_overrides: Record<string, CompartmentShare[]>;
  allocations: Allocation[];
  source: "dispatcher" | "suggestion";
  suggestion_id: string | null;
}

export interface CheckScope {
  truck_id: string;
  load_id?: string | null;
  order_id?: string | null;
}

export interface FixLink {
  kind:
    | "driver"
    | "asset"
    | "order"
    | "hos_override"
    | "compartment"
    | "terminal"
    | "move_back";
  id: string;
  truck_id?: string | null;
  load_id?: string | null;
}

export interface Check {
  check: CheckId;
  outcome: CheckOutcome;
  reason_code: string;
  message: string;
  source: string;
  scope: CheckScope;
  warning_id: string | null;
  fix_link: FixLink | null;
}

export interface WarningAck {
  reason: string;
  actor_user_id: string;
  at: string;
  truck_id: string;
}

export interface PublishedPlan {
  plan_id: string;
  route_id: string;
  run_id: string;
  revision: number;
}

/** `PublishResult`; `recovery`, `rolled_back` and `dropped_orders` drive the Recovering UI (K8.4). */
export interface PublishResult {
  state: string | null;
  stage: string | null;
  reason: string | null;
  writes_made: boolean | "unknown" | null;
  retryable: boolean | null;
  failures: Record<string, unknown>[];
  rolled_back: boolean | null;
  recovery: string | null;
  dropped_orders: string[];
  notifications_failed: string[];
  order_id: string | null;
  observed_status: string | null;
  kept_completed_stops: string[];
  drivers_without_routes: string[];
  group_truck_ids: string[];
  publish_id: string | null;
  at: string | null;
}

export interface RedispatchAttempt {
  attempt_id: string;
  group_truck_ids: string[];
  phase:
    | "claimed"
    | "retire"
    | "stage_relink"
    | "apply"
    | "amend"
    | "notify"
    | "finalize";
  loads: Record<string, Record<string, unknown>>;
  relinks: Record<string, unknown>[];
  notify_baseline: Record<string, string[]>;
  recovery: "none" | "rollback" | "forward";
}

export interface LanePublish {
  state: "draft" | "publishing" | "published" | "failed";
  attempt_id: string | null;
  lease_until: string | null;
  published_version: number | null;
  published_content: {
    driver_id: string | null;
    loads: Load[];
    shelf: string[];
  } | null;
  published_hash: string | null;
  plans: Record<string, PublishedPlan>;
  last_result: PublishResult | null;
  attempt: RedispatchAttempt | null;
}

/** R3.4: the DQ qualification that expires first (past dates included). */
export interface QualificationExpiry {
  kind: "cdl" | "medical_card" | "hazmat" | "tanker";
  /** ISO date (YYYY-MM-DD). */
  expires_on: string;
}

export interface DriverSummary {
  driver_id: string;
  name: string | null;
  status: string | null;
  assigned_truck_id: string | null;
  cdl_class: string | null;
  hazmat_endorsement: boolean | null;
  /** R3.4, from the DQ record; null (or absent) when the driver has none. */
  tanker_endorsement?: boolean | null;
  nearest_expiry?: QualificationExpiry | null;
  eligible: boolean | null;
  ineligible_reasons: string[];
  hos: Record<string, unknown> | null;
  paired_truck_id: string | null;
}

/** K5.2: the truck's permanent driver, offered as "Pair {name}". */
export interface SuggestedDriver {
  driver_id: string;
  name: string;
  source: "assigned_truck_id";
}

export interface CompartmentView {
  compartment_id: string;
  position_index: number;
  capacity_l: number;
  accepts_products: string[];
  state: string | null;
  last_loaded_product: string | null;
}

export interface LaneView {
  truck_id: string;
  /** R2.8: asset subtype; sent on snapshot reads only, kept across lane updates. */
  truck_type?: string | null;
  version: number;
  driver_id: string | null;
  driver: DriverSummary | null;
  suggested_driver: SuggestedDriver | null;
  compartments: CompartmentView[];
  loads: Load[];
  shelf: string[];
  checks: Check[];
  checks_computed_at: string | null;
  checks_stale: boolean;
  outcome: CheckOutcome;
  publish: LanePublish;
  state: LaneState;
  modified: boolean;
  ever_published: boolean;
}

// ─── Snapshot (K5) ───────────────────────────────────────────────────────────

export interface TrayOrder {
  order_id: string;
  customer_id: string | null;
  customer_name: string | null;
  product_code: string | null;
  gallons_requested: number | null;
  fill_to_full: boolean;
  delivery_window_start: string | null;
  delivery_window_end: string | null;
  call_type: string | null;
  status: string | null;
  priority_score: number | null;
  priority_bucket: string | null;
  /** `false` for on-hold orders; the UI never starts a drag for them (R3.6). */
  draggable: boolean;
  block_reason: string | null;
  missing_window: boolean;
  dyed: boolean;
}

export interface TrayTruck {
  truck_id: string;
  compartment_count: number;
  capacity_l: number;
}

export interface Trays {
  orders: TrayOrder[];
  orders_truncated: boolean;
  drivers: DriverSummary[];
  trucks: TrayTruck[];
}

export interface BoardShift {
  id: string;
  start: string;
  end: string;
}

export interface BoardSuggestion {
  suggestion_id: string;
  plan_id: string;
  truck_id: string;
  status: string | null;
  agent: string;
  agent_name: string;
  route_agent_name: string | null;
  route_id: string | null;
  created_at: string | null;
  approval_action_id: string | null;
  loads: Record<string, unknown>[];
  why: Record<string, unknown> | string[] | string | null;
  diff: Record<string, unknown>;
}

export interface BoardSnapshot {
  service_date: string;
  timezone: string;
  mode: BoardMode;
  draft_version: number;
  read_only: boolean;
  read_only_reason: "past_service_day" | "shadow" | null;
  degraded_sources: string[];
  shifts: BoardShift[];
  lanes: LaneView[];
  trays: Trays;
  suggestions: BoardSuggestion[];
  acknowledged: Record<string, WarningAck>;
}

/** Server-side tray filters; sent only after a truncated response (K5). */
export interface BoardTrayFilters {
  call_type?: BoardCallType;
  product?: string;
  window?: WindowFilter;
}

export interface GetBoardOptions {
  /** `?lanes=T1,T2`: only these lanes (after `board_lane_stale`). */
  lanes?: string[];
  filters?: BoardTrayFilters;
  signal?: AbortSignal;
}

// ─── Validate (K3.5) ─────────────────────────────────────────────────────────

export interface DragItem {
  kind: "order" | "stop" | "load" | "driver" | "truck";
  ids: string[];
}

export interface CommandTarget {
  /** `null`/absent: best-fit load (K4.3); `"new"`: a new load. */
  load_id?: string | "new" | null;
  /** `null`/absent: best-fit position (R5.1). */
  index?: number | null;
}

export interface ValidateBody {
  item: DragItem;
  candidates: string[];
  position?: CommandTarget | null;
}

export interface CandidatePreview {
  fill_by_compartment: Record<string, number>;
  insertion_index: number | null;
  load_id: string | null;
  eta_delta_minutes: number | null;
}

export interface CandidateResult {
  outcome: CheckOutcome;
  worst_checks: Check[];
  reason: string | null;
  preview: CandidatePreview;
}

export interface ValidateResponse {
  results: Record<string, CandidateResult>;
  degraded_sources: string[];
}

// ─── Commands (K4.1) ─────────────────────────────────────────────────────────

interface CommandBase {
  client_command_id: string;
  expected_lane_versions: Record<string, number>;
  input_modality: InputModality;
}

export type BoardCommand = CommandBase &
  (
    | { type: "add_lane"; truck_id: string }
    | { type: "remove_lane"; truck_id: string }
    | { type: "pair_driver"; truck_id: string; driver_id: string | null }
    | {
        type: "assign_orders";
        order_ids: string[];
        truck_id: string;
        target: CommandTarget;
      }
    | {
        type: "move_stops";
        order_ids: string[];
        truck_id: string;
        target: CommandTarget;
      }
    | { type: "unassign_orders"; order_ids: string[] }
    | { type: "move_load"; load_id: string; truck_id: string; index: number }
    | { type: "set_terminal"; load_id: string; terminal_id: string | null }
    | {
        type: "set_allocation";
        load_id: string;
        order_id: string;
        shares: CompartmentShare[] | null;
      }
    | { type: "set_load_shift"; load_id: string; shift_id: ShiftId }
    | {
        type: "acknowledge_warning";
        truck_id: string;
        warning_id: string;
        reason: string;
      }
    | {
        type: "accept_suggestion";
        suggestion_id: string;
        load_ids?: string[] | null;
      }
    | { type: "discard_lane_changes"; truck_id: string }
    | { type: "revert"; target_command_id: string }
    | { type: "reapply"; target_command_id: string }
  );

export type BoardCommandType = BoardCommand["type"];

export interface CommandResponse {
  draft_version: number;
  lanes: LaneView[];
  checks: Record<string, Check[]>;
  /** The id had already committed; treat as success (K4.5). */
  already_applied: boolean;
  audit_degraded: boolean;
}

// ─── Publish (K7.1) ──────────────────────────────────────────────────────────

export interface PublishLaneRef {
  truck_id: string;
  expected_version: number;
}

export interface PublishBody {
  client_request_id: string;
  lanes: PublishLaneRef[];
  warning_reasons?: Record<string, string>;
}

export interface PublishPreviewBody {
  lanes: PublishLaneRef[];
  warning_reasons?: Record<string, string>;
}

/** One publish group, the same shape in the 202 and the dry run (K8.4). */
export interface PublishGroup {
  truck_ids: string[];
  kind: "first_publish" | "redispatch";
  added_lanes: string[];
}

export interface PublishAccepted {
  publish_id: string | null;
  lanes: { truck_id: string; state: "queued" | "already_published" }[];
  groups: PublishGroup[];
  already_published: string[];
  replayed?: boolean;
}

export interface PublishPreview {
  ready: boolean;
  not_ready: { truck_id: string; reasons: string[] }[];
  groups: PublishGroup[];
  loads: {
    truck_id: string;
    load_id: string;
    class: "unchanged" | "new" | "new_revision" | "amend" | "removed";
    info: string[];
  }[];
  notifications: {
    driver_id: string;
    name: string | null;
    revoke_order_ids: string[];
    assign_order_ids: string[];
    route_updated: boolean;
  }[];
  open_warnings: { truck_id: string; warning_id: string; message: string }[];
  already_published: string[];
}

export interface PublishStatus {
  publish_id: string;
  lanes: {
    truck_id: string;
    state: LaneState;
    last_result: PublishResult | null;
  }[];
  done: boolean;
}

// ─── Suggestions and history ─────────────────────────────────────────────────

export interface RejectSuggestionResult {
  plan_id: string;
  dismissed: boolean;
  approval?: Record<string, unknown> | null;
}

export interface HistoryParams {
  truck_id?: string;
  cursor?: string;
  size?: number;
}

export interface HistoryItem {
  command_id: string;
  type: string;
  result: string;
  actor_user_id: string | null;
  actor_name: string | null;
  input_modality: string | null;
  created_at: string | null;
  lanes: Record<string, unknown>[];
  checks_summary: Record<string, unknown>;
  overrides: Record<string, unknown>[];
}

export interface HistoryPage {
  items: HistoryItem[];
  next_cursor: string | null;
}

// ─── Errors ──────────────────────────────────────────────────────────────────

/** An `ApiError` with the backend's `details` and `details.reason` (K11). */
export class BoardApiError extends ApiError {
  details: Record<string, unknown>;
  reason?: string;

  constructor(
    message: string,
    status: number,
    code?: string,
    details: Record<string, unknown> = {},
  ) {
    super(message, status, code);
    this.name = "BoardApiError";
    this.details = details;
    this.reason =
      typeof details.reason === "string" ? details.reason : undefined;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `details` from the AppException envelope, or from a nested `detail` object. */
function detailsOf(body: unknown): Record<string, unknown> {
  if (!isRecord(body)) return {};
  if (isRecord(body.details)) return body.details;
  if (isRecord(body.detail) && isRecord(body.detail.details)) {
    return body.detail.details;
  }
  return {};
}

/** Builds a `BoardApiError` from a non-OK response, tolerating non-JSON bodies. */
export async function boardErrorFromResponse(
  response: Response,
): Promise<BoardApiError> {
  let body: unknown = {};
  try {
    body = await response.json();
  } catch {
    // Non-JSON error body: fall through to the generic message.
  }
  return new BoardApiError(
    extractApiErrorMessage(body, `HTTP error! status: ${response.status}`),
    response.status,
    extractApiErrorCode(body),
    detailsOf(body),
  );
}

/** Error code of any thrown value (duck typed, so partial mocks still work). */
export function boardErrorCode(err: unknown): string | undefined {
  if (isRecord(err) && typeof err.code === "string") return err.code;
  return undefined;
}

/** `details.reason` of a `BoardApiError`, if any. */
export function boardErrorReason(err: unknown): string | undefined {
  if (!isRecord(err)) return undefined;
  if (typeof err.reason === "string") return err.reason;
  if (isRecord(err.details) && typeof err.details.reason === "string") {
    return err.details.reason;
  }
  return undefined;
}

/** HTTP status of any thrown value; 0 for network errors, undefined if unknown. */
export function boardErrorStatus(err: unknown): number | undefined {
  if (isRecord(err) && typeof err.status === "number") return err.status;
  return undefined;
}

/** True for a caller-initiated abort (never shown to the user). */
export function isAbortError(err: unknown): boolean {
  return err instanceof Error && err.name === "AbortError";
}

// ─── Ids ─────────────────────────────────────────────────────────────────────

/** A uuid4 for `client_command_id` / `client_request_id` (the backend requires uuid form). */
export function newClientId(): string {
  const c = (globalThis as { crypto?: Crypto }).crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === "function") {
    c.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex
    .slice(6, 8)
    .join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10, 16).join("")}`;
}

// ─── HTTP helpers ────────────────────────────────────────────────────────────

/**
 * `fetchWithTimeout` that also honours a caller's `AbortSignal`. A caller abort
 * rethrows the `AbortError` (so callers can ignore it); only the timeout
 * becomes an `ApiTimeoutError`.
 */
function fetcherWithSignal(external?: AbortSignal) {
  return async (
    url: string,
    options: RequestInit = {},
    timeout: number = API_TIMEOUTS.STANDARD,
  ): Promise<Response> => {
    const controller = new AbortController();
    let timedOut = false;
    const timeoutId = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeout);
    const onAbort = () => controller.abort();
    if (external) {
      if (external.aborted) controller.abort();
      else external.addEventListener("abort", onAbort);
    }
    try {
      return await fetch(url, { ...options, signal: controller.signal });
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError" && timedOut) {
        throw new ApiTimeoutError(
          `Request timed out after ${timeout / 1000} seconds`,
        );
      }
      throw error;
    } finally {
      clearTimeout(timeoutId);
      external?.removeEventListener("abort", onAbort);
    }
  };
}

async function boardRequest<T>(
  endpoint: string,
  options: RequestInit = {},
  signal?: AbortSignal,
): Promise<T> {
  const url = `${API_BASE_URL}${BOARD_BASE}${endpoint}`;
  try {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options.headers as Record<string, string> | undefined),
    };
    // Session cookie + anti-CSRF token are attached by the SuperTokens SDK.
    const response = await fetchWithSession(fetcherWithSignal(signal), url, {
      ...options,
      headers,
    });
    if (!response.ok) {
      throw await boardErrorFromResponse(response);
    }
    const body = (await response.json()) as { data: T };
    return body.data;
  } catch (error) {
    if (
      error instanceof ApiTimeoutError ||
      error instanceof ApiError ||
      isAbortError(error)
    ) {
      throw error;
    }
    throw new BoardApiError(
      error instanceof Error ? error.message : "Unknown error",
      0,
    );
  }
}

function day(serviceDate: string): string {
  return `/${encodeURIComponent(serviceDate)}`;
}

function post(body: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(body) };
}

// ─── Endpoints (K11) ─────────────────────────────────────────────────────────

/** GET /status: mode for tab visibility. 404 (disabled) or 403 hides the tab. */
export async function getBoardStatus(
  signal?: AbortSignal,
): Promise<{ mode: BoardMode }> {
  return boardRequest<{ mode: BoardMode }>("/status", {}, signal);
}

/** GET /{service_date}: the board snapshot (K5). */
export async function getBoard(
  serviceDate: string,
  options: GetBoardOptions = {},
): Promise<BoardSnapshot> {
  const qs = buildQueryString({
    lanes: options.lanes?.length ? options.lanes.join(",") : undefined,
    call_type: options.filters?.call_type,
    product: options.filters?.product,
    window: options.filters?.window,
  });
  return boardRequest<BoardSnapshot>(
    `${day(serviceDate)}${qs}`,
    {},
    options.signal,
  );
}

/** POST /{service_date}/validate: drag-time dry run (K3.5). */
export async function validateBoard(
  serviceDate: string,
  body: ValidateBody,
  signal?: AbortSignal,
): Promise<ValidateResponse> {
  return boardRequest<ValidateResponse>(
    `${day(serviceDate)}/validate`,
    post(body),
    signal,
  );
}

/** POST /{service_date}/commands: one command (K4). */
export async function sendBoardCommand(
  serviceDate: string,
  command: BoardCommand,
): Promise<CommandResponse> {
  return boardRequest<CommandResponse>(
    `${day(serviceDate)}/commands`,
    post(command),
  );
}

/** POST /{service_date}/publish: start a publish (202). */
export async function publishBoard(
  serviceDate: string,
  body: PublishBody,
): Promise<PublishAccepted> {
  return boardRequest<PublishAccepted>(
    `${day(serviceDate)}/publish`,
    post({ ...body, dry_run: false }),
  );
}

/** POST /{service_date}/publish with `dry_run: true`: the review dialog data (K7.1). */
export async function previewPublish(
  serviceDate: string,
  body: PublishPreviewBody,
  signal?: AbortSignal,
): Promise<PublishPreview> {
  return boardRequest<PublishPreview>(
    `${day(serviceDate)}/publish`,
    post({ ...body, dry_run: true }),
    signal,
  );
}

/** GET /{service_date}/publish/{publish_id}: publish status. */
export async function getPublish(
  serviceDate: string,
  publishId: string,
): Promise<PublishStatus> {
  return boardRequest<PublishStatus>(
    `${day(serviceDate)}/publish/${encodeURIComponent(publishId)}`,
  );
}

/** POST /{service_date}/suggestions/{plan_id}/reject (K9). */
export async function rejectSuggestion(
  serviceDate: string,
  planId: string,
  reason?: string,
): Promise<RejectSuggestionResult> {
  const trimmed = reason?.trim();
  return boardRequest<RejectSuggestionResult>(
    `${day(serviceDate)}/suggestions/${encodeURIComponent(planId)}/reject`,
    post(trimmed ? { reason: trimmed } : {}),
  );
}

/** GET /{service_date}/history: command log, newest first (R22.3). */
export async function getBoardHistory(
  serviceDate: string,
  params: HistoryParams = {},
): Promise<HistoryPage> {
  return boardRequest<HistoryPage>(
    `${day(serviceDate)}/history${buildQueryString(params)}`,
  );
}
