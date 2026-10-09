/**
 * Pure view helpers for the Work screen (UI revamp task 4.3). No requests and
 * no state: they only arrange what `GET /api/driver/work` already returned.
 */

import type { FuelOrder, RouteStop } from '@/types/order';

/**
 * The delivery being run first, then the rest by window start. The same order
 * the Route screen's stop chips use.
 */
export function sortWork(orders: FuelOrder[]): FuelOrder[] {
  return [...orders].sort((left, right) => {
    if (left.status !== right.status) {
      if (left.status === 'in_transit') return -1;
      if (right.status === 'in_transit') return 1;
    }
    return new Date(left.delivery_window_start).getTime() - new Date(right.delivery_window_start).getTime();
  });
}

export interface ProductLine {
  grade: string;
  gallons: number;
}

/**
 * One line per grade: the manifest's planned gallons summed by grade when the
 * detail is loaded, else the order's own grade and ordered gallons.
 */
export function productLines(order: FuelOrder): ProductLine[] {
  const manifest = order.manifest_available === false ? [] : order.compartment_manifest ?? [];
  if (manifest.length > 0) {
    const byGrade = new Map<string, number>();
    for (const entry of manifest) {
      const gallons = Number.isFinite(entry.planned_gallons) ? entry.planned_gallons : 0;
      byGrade.set(entry.product_grade, (byGrade.get(entry.product_grade) ?? 0) + gallons);
    }
    return [...byGrade.entries()].map(([grade, gallons]) => ({ grade, gallons }));
  }
  return order.product_grade ? [{ grade: order.product_grade, gallons: order.ordered_gallons }] : [];
}

/**
 * Planned US gallons for the order, whole: the manifest total when the plan
 * resolved one, else the ordered gallons. `null` when neither is a positive
 * number, so the POD field stays empty rather than showing 0.
 */
export function plannedGallons(order: FuelOrder): number | null {
  const total = productLines(order).reduce((sum, line) => sum + (Number.isFinite(line.gallons) ? line.gallons : 0), 0);
  return total > 0 ? Math.round(total) : null;
}

/** The first stop not yet completed, in sequence order. */
export function nextPendingStop(order: FuelOrder | null | undefined): RouteStop | null {
  const stops = [...(order?.stops ?? [])].sort((a, b) => a.sequence - b.sequence);
  return stops.find((stop) => stop.status !== 'completed') ?? null;
}
