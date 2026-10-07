/**
 * Optimistic pre-check (design K3.8, D2).
 *
 * Colours a drop target only until the server's validate answer arrives, and
 * is discarded when it does. It reads only fields the server computed
 * (`draggable`/`block_reason`, `accepts_products`, `capacity_l`, allocations,
 * driver `eligible`/`ineligible_reasons`, lane state) and holds no rule logic
 * of its own: no product compatibility tables, no HOS or window maths.
 *
 * It returns `likely_block` or `unknown`, never a pass: only the server can
 * say a drop is fine.
 */
import type {
  BoardSnapshot,
  DriverSummary,
  LaneView,
  TrayOrder,
} from "../../../services/dispatchBoardApi";

export type PrecheckResult =
  | { kind: "likely_block"; reason: string }
  | { kind: "unknown" };

/** The same factor `services/fuelApi.ts` uses (`litersToGallons`). */
const LITERS_PER_GALLON = 3.78541;

const UNKNOWN: PrecheckResult = { kind: "unknown" };

function block(reason: string): PrecheckResult {
  return { kind: "likely_block", reason };
}

/** Lanes that refuse every command while they are in these states (K2.1). */
function laneLocked(lane: LaneView): PrecheckResult | null {
  if (lane.state === "publishing") return block("publishing");
  if (lane.state === "recovering") return block("recovery_pending");
  return null;
}

function orderFits(order: TrayOrder, lane: LaneView): PrecheckResult {
  if (order.draggable === false) {
    return block(order.block_reason ?? "not_draggable");
  }
  const product = order.product_code;
  const listed = lane.compartments.filter((c) => c.accepts_products.length > 0);
  if (
    product &&
    lane.compartments.length > 0 &&
    listed.length === lane.compartments.length &&
    !listed.some((c) => c.accepts_products.includes(product))
  ) {
    return block("no_compatible_compartments");
  }
  if (
    !order.fill_to_full &&
    order.gallons_requested &&
    lane.compartments.length
  ) {
    // Best fit may open a new load (K4.3), so only an order bigger than the
    // whole empty truck is a likely block; partly full loads are the server's call.
    const capacity = lane.compartments.reduce(
      (sum, c) => sum + c.capacity_l,
      0,
    );
    if (order.gallons_requested * LITERS_PER_GALLON > capacity) {
      return block("capacity_shortfall");
    }
  }
  return UNKNOWN;
}

function driverFits(driver: DriverSummary): PrecheckResult {
  if (driver.eligible === false) {
    return block(driver.ineligible_reasons[0] ?? "driver_ineligible");
  }
  return UNKNOWN;
}

export type PrecheckItem =
  | { kind: "order"; order: TrayOrder }
  | { kind: "driver"; driver: DriverSummary }
  | { kind: "stop" | "load" | "truck"; ids: string[] };

/** Best-effort guess for one item over one lane. Never `pass`. */
export function precheck(
  item: PrecheckItem,
  lane: LaneView,
  snapshot?: Pick<BoardSnapshot, "read_only"> | null,
): PrecheckResult {
  if (snapshot?.read_only) return block("read_only");
  const locked = laneLocked(lane);
  if (locked) return locked;
  switch (item.kind) {
    case "order":
      return orderFits(item.order, lane);
    case "driver":
      return driverFits(item.driver);
    default:
      return UNKNOWN;
  }
}

/** `precheck` over every lane, keyed by truck id. */
export function precheckLanes(
  item: PrecheckItem,
  lanes: LaneView[],
  snapshot?: Pick<BoardSnapshot, "read_only"> | null,
): Record<string, PrecheckResult> {
  const out: Record<string, PrecheckResult> = {};
  for (const lane of lanes) out[lane.truck_id] = precheck(item, lane, snapshot);
  return out;
}
