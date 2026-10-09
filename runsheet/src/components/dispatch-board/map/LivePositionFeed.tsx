/**
 * Live truck positions for the board (R15.4, R17.1) from the existing
 * `/ws/fleet/live` feed (`useFleetWebSocket`). Rendered only while a
 * position is needed (today with a published lane, or a map on screen), so
 * the board opens no fleet socket otherwise.
 */
import { useMemo } from "react";
import {
  type LocationUpdateData,
  useFleetWebSocket,
} from "../../../hooks/useFleetWebSocket";
import type { TruckPosition } from "./runningLate";

export interface LivePositionFeedProps {
  onPositions: (positions: Record<string, TruckPosition>) => void;
}

function toPositions(
  updates: LocationUpdateData[],
): Record<string, TruckPosition> {
  const out: Record<string, TruckPosition> = {};
  for (const u of updates) {
    const at = Date.parse(u.timestamp);
    const { lat, lon } = u.coordinates ?? {};
    if (typeof lat !== "number" || typeof lon !== "number") continue;
    out[u.truck_id] = { lat, lon, at: Number.isNaN(at) ? Date.now() : at };
  }
  return out;
}

export function LivePositionFeed({ onPositions }: LivePositionFeedProps) {
  const options = useMemo(
    () => ({
      autoConnect: true,
      onLocationUpdate: (u: LocationUpdateData) =>
        onPositions(toPositions([u])),
      onBatchLocationUpdate: (us: LocationUpdateData[]) =>
        onPositions(toPositions(us)),
    }),
    [onPositions],
  );
  useFleetWebSocket(options);
  return null;
}

export default LivePositionFeed;
