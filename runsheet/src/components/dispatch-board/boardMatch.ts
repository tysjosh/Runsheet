/**
 * Filter chips and search applied to cards (R4.1, R4.2). Nothing is removed:
 * a card that matches the search is highlighted, a card that fails a filter
 * or the search is dimmed. Pure functions so trays and lanes agree.
 */
import type {
  DriverSummary,
  LaneView,
  Stop,
  TrayOrder,
} from "../../services/dispatchBoardApi";
import { windowBucket } from "./boardTime";
import type { BoardFilters } from "./viewState";

/** How a card is drawn: `match` = highlighted, `dim` = dimmed, `plain` = neither. */
export type MatchState = "match" | "dim" | "plain";

export interface MatchContext {
  filters: BoardFilters;
  search: string;
  serviceDate: string;
  timeZone: string;
  now?: number;
}

interface OrderFacts {
  orderId: string;
  customerName: string | null;
  callType: string | null;
  product: string | null;
  priority: string | null;
  status: string | null;
  windowStart: string | null;
  windowEnd: string | null;
  hasWarnings: boolean;
}

function norm(text: string | null | undefined): string {
  return (text ?? "").trim().toLowerCase();
}

function inList(values: string[], value: string | null): boolean {
  return values.length === 0 || (value !== null && values.includes(value));
}

function passesFilters(f: OrderFacts, ctx: MatchContext): boolean {
  const { filters } = ctx;
  if (!inList(filters.call_type, f.callType)) return false;
  if (!inList(filters.product, f.product)) return false;
  if (!inList(filters.priority, f.priority)) return false;
  if (!inList(filters.status, f.status)) return false;
  if (filters.window.length > 0) {
    const bucket = windowBucket(
      f.windowStart,
      f.windowEnd,
      ctx.serviceDate,
      ctx.timeZone,
      ctx.now,
    );
    if (!bucket || !filters.window.includes(bucket)) return false;
  }
  if (filters.has_warnings && !f.hasWarnings) return false;
  return true;
}

/** Filters only dim; a search hit on a card that passes the filters highlights it. */
function decide(passes: boolean, searchHit: boolean | null): MatchState {
  if (!passes) return "dim";
  if (searchHit === true) return "match";
  if (searchHit === false) return "dim";
  return "plain";
}

function searchHit(
  query: string,
  ...fields: (string | null | undefined)[]
): boolean | null {
  const q = norm(query);
  if (!q) return null;
  return fields.some((f) => norm(f).includes(q));
}

/** A tray order card (R3.2 fields). */
export function trayOrderMatch(
  order: TrayOrder,
  ctx: MatchContext,
): MatchState {
  const facts: OrderFacts = {
    orderId: order.order_id,
    customerName: order.customer_name,
    callType: order.call_type,
    product: order.product_code,
    priority: order.priority_bucket,
    status: order.status,
    windowStart: order.delivery_window_start,
    windowEnd: order.delivery_window_end,
    // Unplanned orders carry no checks yet.
    hasWarnings: false,
  };
  return decide(
    passesFilters(facts, ctx),
    searchHit(ctx.search, order.order_id, order.customer_name),
  );
}

/** A stop card on a lane. Priority is not on stops, so that chip is ignored here. */
export function stopMatch(
  stop: Stop,
  lane: LaneView,
  ctx: MatchContext,
): MatchState {
  const facts: OrderFacts = {
    orderId: stop.order_id,
    customerName: null,
    callType: stop.snapshot.call_type,
    product: stop.snapshot.product_code,
    priority: null,
    status: stop.snapshot.status,
    windowStart: stop.snapshot.window.start,
    windowEnd: stop.snapshot.window.end,
    hasWarnings: lane.checks.some(
      (c) =>
        (c.outcome === "warn" || c.outcome === "block") &&
        (c.scope.order_id === stop.order_id || !c.scope.order_id),
    ),
  };
  const filters = { ...ctx.filters, priority: [] };
  return decide(
    passesFilters(facts, { ...ctx, filters }),
    searchHit(
      ctx.search,
      stop.order_id,
      lane.truck_id,
      lane.driver?.name ?? null,
    ),
  );
}

/** The lane header: highlighted when the truck or driver matches the search. */
export function laneMatch(lane: LaneView, search: string): MatchState {
  const hit = searchHit(search, lane.truck_id, lane.driver?.name ?? null);
  if (hit === null) return "plain";
  return hit ? "match" : "plain";
}

export function driverMatch(driver: DriverSummary, search: string): MatchState {
  const hit = searchHit(search, driver.name, driver.driver_id);
  if (hit === null) return "plain";
  return hit ? "match" : "dim";
}

/** Classes for a card in each state (ring + opacity; the text suffix carries meaning too). */
export function matchClass(state: MatchState): string {
  if (state === "match") return "ring-2 ring-primary";
  if (state === "dim") return "opacity-40";
  return "";
}

/** Accessible suffix so the state isn't visual only. */
export function matchSuffix(state: MatchState): string {
  if (state === "match") return ", matches";
  if (state === "dim") return ", doesn't match the filters";
  return "";
}
