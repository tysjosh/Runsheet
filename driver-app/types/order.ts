/**
 * The ONE order type for the Runsheet driver app (R16.14).
 *
 * The donor application carried two divergent `RiderOrder` declarations. This
 * module replaces both with a single `FuelOrder`, and it is the only order type
 * in the tree: screens, `components/DispatchOrderCard.tsx`, the query layer, and
 * the offline queue all read this shape.
 *
 * Field names match the wire shape returned by `GET /api/driver/work` and
 * `GET /api/driver/work/{order_id}` verbatim, so there is no mapping layer to
 * drift out of sync with the backend and no second "view" type. The list
 * endpoint resolves no plans, so the manifest and stop-sequence fields are
 * optional: a list item and a detail document are the same type, one carrying
 * fewer fields.
 *
 * Every volume on this type is US gallons (R16.10, R16.19) — there is no litre
 * field, and `lib/units.ts` exposes no litre formatter, so a litre value has no
 * path into the UI.
 *
 * Requirements: 16.14, 16.10, 16.18, 16.19, 15.7
 */

/** `fuel/order_models.py:48-51` `OrderStatus`, verbatim. */
export type OrderStatus =
  | 'placed'
  | 'confirmed'
  | 'scheduled'
  | 'dispatched'
  | 'in_transit'
  | 'delivered'
  | 'failed'
  | 'cancelled'
  | 'on_hold';

/**
 * The statuses past which an order does no more work. Cached customer data for
 * an order in one of these statuses is deleted within 24 hours (R15.7) — see
 * `lib/customer-cache.ts`.
 *
 * `on_hold` is deliberately absent: a held order can resume, so its customer
 * contact is still needed.
 */
export const TERMINAL_ORDER_STATUSES = ['delivered', 'failed', 'cancelled'] as const;

export type TerminalOrderStatus = (typeof TERMINAL_ORDER_STATUSES)[number];

/** Narrowing predicate for {@link TERMINAL_ORDER_STATUSES}. */
export function isTerminalOrderStatus(status: string): status is TerminalOrderStatus {
  return (TERMINAL_ORDER_STATUSES as readonly string[]).includes(status);
}

/**
 * The only volume unit this app sends or receives (R6.14, R16.18). The app
 * performs no volume-unit conversion of its own; the gallons→litres boundary
 * lives server-side in `Agents/support/volume_units.py`.
 */
export const QUANTITY_UNIT = 'us_gallon';

export type QuantityUnit = typeof QUANTITY_UNIT;

/** Latitude/longitude pair, as sent on every geotagged driver action. */
export interface GeoPoint {
  lat: number;
  lon: number;
}

export interface OrderDestination extends GeoPoint {
  address: string;
}

/** One compartment of the resolved loading plan (R3.8). */
export interface CompartmentManifestEntry {
  compartment_id: string;
  product_grade: string;
  planned_gallons: number;
  prior_product_grade: string | null;
  cross_contamination_warning: boolean;
  last_cleaned_at: string | null;
}

/** Completion state of a route stop, from `mvp_plan_executions.stops[].status`. */
export type StopStatus = 'pending' | 'completed';

/** One stop of the resolved route plan (R3.10). */
export interface RouteStop extends GeoPoint {
  sequence: number;
  station_id: string;
  planned_arrival: string | null;
  /** Grade → US gallons planned for this stop. Never litres. */
  planned_gallons_by_grade: Record<string, number>;
  status: StopStatus;
}

/**
 * A fuel order as the driver surface returns it.
 *
 * Detail-only fields are optional because the list endpoint resolves no plans.
 * `customer_phone` is `null` or absent when the session lacks PII access
 * (R15.6), so callers must treat it as missing rather than empty.
 */
export interface FuelOrder {
  order_id: string;
  status: OrderStatus;
  delivery_window_start: string;
  delivery_window_end: string;
  destination: OrderDestination;
  customer_name: string;
  customer_phone?: string | null;
  product_grade: string;
  /** US gallons ordered. */
  ordered_gallons: number;
  quantity_unit: QuantityUnit;

  // ---- detail endpoint only ------------------------------------------------
  manifest_available?: boolean;
  compartment_manifest?: CompartmentManifestEntry[];
  route_available?: boolean;
  stops?: RouteStop[];

  /**
   * The plan and route this order's stops belong to, which
   * `POST /api/fuel/mvp/plan/{plan_id}/checkin` is keyed on.
   *
   * Optional because the current `GET /api/driver/work/{order_id}` projection
   * resolves the plan and the route but returns neither identifier. When they
   * are absent the route screen says the stop check-in is unavailable for this
   * assignment rather than guessing a plan reference — the same explicit
   * degradation `manifest_available` and `route_available` already use (R3.11).
   */
  plan_id?: string | null;
  route_id?: string | null;
}
