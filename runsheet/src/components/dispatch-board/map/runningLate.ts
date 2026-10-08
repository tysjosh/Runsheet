/**
 * "Running late" (R15.4): a published lane whose truck, from its last
 * telematics position, would reach its next stop more than the threshold
 * (15 minutes, the tenant default) after that stop's route ETA.
 *
 * Projection: now + straight-line distance to the next stop at an average
 * road speed. Pure, so the badge is testable without a socket or a clock.
 */
import type { LaneView, LatLon } from "../../../services/dispatchBoardApi";

/** Minutes behind the route ETA before the badge shows (R15.4 default). */
export const LATE_THRESHOLD_MINUTES = 15;
/** Average road speed used to project arrival from the live position. */
export const PROJECTION_SPEED_KMH = 50;
/** Positions older than this are not used (the truck may have moved). */
export const POSITION_MAX_AGE_MS = 30 * 60_000;

export interface TruckPosition extends LatLon {
  /** Epoch ms of the telematics reading. */
  at: number;
}

const FINISHED = new Set(["delivered", "failed", "cancelled"]);

/** Great-circle distance in km. */
export function distanceKm(a: LatLon, b: LatLon): number {
  const r = 6371;
  const rad = (d: number) => (d * Math.PI) / 180;
  const dLat = rad(b.lat - a.lat);
  const dLon = rad(b.lon - a.lon);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * r * Math.asin(Math.min(1, Math.sqrt(h)));
}

/**
 * Whole minutes late beyond the threshold for a published lane, else `null`
 * (not published, no position, stale position, no next stop with an ETA and
 * a location, or within the threshold).
 */
export function minutesLate(
  lane: LaneView,
  position: TruckPosition | undefined,
  nowMs: number,
  threshold = LATE_THRESHOLD_MINUTES,
): number | null {
  if (!position || Object.keys(lane.publish.plans).length === 0) return null;
  if (nowMs - position.at > POSITION_MAX_AGE_MS) return null;
  const next = lane.loads
    .flatMap((l) => l.stops)
    .find((s) => !FINISHED.has(s.snapshot.status ?? ""));
  if (!next?.eta || !next.location) return null;
  const eta = Date.parse(next.eta);
  if (Number.isNaN(eta)) return null;
  const travelMs =
    (distanceKm(position, next.location) / PROJECTION_SPEED_KMH) * 3_600_000;
  const late = Math.round((nowMs + travelMs - eta) / 60_000);
  return late > threshold ? late : null;
}
