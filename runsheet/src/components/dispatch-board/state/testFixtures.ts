/**
 * Shared Dispatch Board test fixtures (Jest only). Builders return complete
 * objects of the API shapes so tests override only what they care about.
 */
import type {
  BoardSnapshot,
  Check,
  CompartmentView,
  DriverSummary,
  LaneView,
  Load,
  Stop,
  TrayOrder,
} from "../../../services/dispatchBoardApi";

export function makeStop(orderId: string, over: Partial<Stop> = {}): Stop {
  return {
    order_id: orderId,
    snapshot: {
      product_code: "ULSD",
      customer_id: "C1",
      customer_tank_id: null,
      gallons_requested: 3000,
      fill_to_full: false,
      window: { start: null, end: null },
      call_type: "one_off",
      status: "confirmed",
    },
    location: null,
    eta: null,
    ...over,
  };
}

export function makeLoad(
  loadId: string,
  orderIds: string[],
  over: Partial<Load> = {},
): Load {
  return {
    load_id: loadId,
    shift_id: "day",
    terminal_id: null,
    planned_start: null,
    planned_end: null,
    stops: orderIds.map((id) => makeStop(id)),
    allocation_overrides: {},
    allocations: [],
    source: "dispatcher",
    suggestion_id: null,
    ...over,
  };
}

export function makeCompartment(
  id: string,
  over: Partial<CompartmentView> = {},
): CompartmentView {
  return {
    compartment_id: id,
    position_index: 0,
    capacity_l: 10000,
    accepts_products: [],
    state: null,
    last_loaded_product: null,
    ...over,
  };
}

export function makeCheck(over: Partial<Check> = {}): Check {
  return {
    check: "delivery_window",
    outcome: "warn",
    reason_code: "eta_after_window",
    message: "Delivery window at risk",
    source: "dispatch_board_eta",
    scope: { truck_id: "T1" },
    warning_id: "0123456789abcdef",
    fix_link: null,
    ...over,
  };
}

export function makeLane(
  truckId: string,
  version = 1,
  over: Partial<LaneView> = {},
): LaneView {
  return {
    truck_id: truckId,
    version,
    driver_id: null,
    driver: null,
    suggested_driver: null,
    compartments: [makeCompartment(`${truckId}-c1`)],
    loads: [],
    shelf: [],
    checks: [],
    checks_computed_at: null,
    checks_stale: false,
    outcome: "pass",
    publish: {
      state: "draft",
      attempt_id: null,
      lease_until: null,
      published_version: null,
      published_content: null,
      published_hash: null,
      plans: {},
      last_result: null,
      attempt: null,
    },
    state: "draft",
    modified: false,
    ever_published: false,
    ...over,
  };
}

export function makeTrayOrder(
  orderId: string,
  over: Partial<TrayOrder> = {},
): TrayOrder {
  return {
    order_id: orderId,
    customer_id: "C1",
    customer_name: "Customer",
    product_code: "ULSD",
    gallons_requested: 3000,
    fill_to_full: false,
    delivery_window_start: null,
    delivery_window_end: null,
    call_type: "one_off",
    status: "confirmed",
    priority_score: null,
    priority_bucket: null,
    draggable: true,
    block_reason: null,
    missing_window: false,
    dyed: false,
    ...over,
  };
}

export function makeDriver(
  driverId: string,
  over: Partial<DriverSummary> = {},
): DriverSummary {
  return {
    driver_id: driverId,
    name: `Driver ${driverId}`,
    status: "active",
    assigned_truck_id: null,
    cdl_class: "A",
    hazmat_endorsement: true,
    eligible: true,
    ineligible_reasons: [],
    hos: null,
    paired_truck_id: null,
    ...over,
  };
}

export function makeSnapshot(over: Partial<BoardSnapshot> = {}): BoardSnapshot {
  return {
    service_date: "2026-10-08",
    timezone: "America/Chicago",
    mode: "active_gated",
    draft_version: 1,
    read_only: false,
    read_only_reason: null,
    degraded_sources: [],
    shifts: [
      { id: "day", start: "06:00", end: "18:00" },
      { id: "night", start: "18:00", end: "06:00" },
    ],
    lanes: [],
    trays: { orders: [], orders_truncated: false, drivers: [], trucks: [] },
    suggestions: [],
    acknowledged: {},
    ...over,
  };
}
