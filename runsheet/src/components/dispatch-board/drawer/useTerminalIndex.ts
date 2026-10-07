/**
 * Tenant terminals for the drawer's terminal picker and the map's terminal
 * markers (R5.6, R17.1), read once from the existing `GET /api/fuel/terminals`
 * when first needed.
 */
import { useEffect, useState } from "react";
import type { LatLon } from "../../../services/dispatchBoardApi";
import { listTerminals, type Terminal } from "../../../services/fuelApi";

export interface TerminalIndex {
  terminals: Terminal[];
  coords: Record<string, LatLon>;
  /** `true` once loaded (or failed). */
  ready: boolean;
  failed: boolean;
}

const EMPTY: TerminalIndex = {
  terminals: [],
  coords: {},
  ready: false,
  failed: false,
};

export function useTerminalIndex(enabled: boolean): TerminalIndex {
  const [index, setIndex] = useState<TerminalIndex>(EMPTY);
  const wanted = enabled && !index.ready;
  useEffect(() => {
    if (!wanted) return;
    let cancelled = false;
    listTerminals({ status: "active", size: 200 })
      .then((res) => {
        if (cancelled) return;
        const coords: Record<string, LatLon> = {};
        for (const t of res.items) {
          if (
            typeof t.location_lat === "number" &&
            typeof t.location_lon === "number"
          ) {
            coords[t.terminal_id] = {
              lat: t.location_lat,
              lon: t.location_lon,
            };
          }
        }
        setIndex({ terminals: res.items, coords, ready: true, failed: false });
      })
      .catch(() => {
        if (!cancelled) setIndex({ ...EMPTY, ready: true, failed: true });
      });
    return () => {
      cancelled = true;
    };
  }, [wanted]);
  return index;
}
