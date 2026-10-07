"use client";
/**
 * Route map for one lane (the drawer's Map tab) or every visible lane (the
 * split view) (design K14.10, R17.1–R17.4, R15.4).
 *
 * Draws each load's terminal, numbered stops in order, a polyline in stop
 * order per lane (12-colour palette; lanes past 12 reuse a colour with a
 * dashed line), and the truck's live position. Selecting a stop marker
 * selects it on the board, and the board selection highlights the marker
 * (R17.2). The map is never a drop target (R17.4).
 *
 * The same stops are listed under the map as buttons, so selection works by
 * keyboard and screen reader too. Without a Google Maps key the map shows an
 * empty state instead.
 */
import {
  AdvancedMarker,
  APIProvider,
  Map as GoogleMap,
  useMap,
} from "@vis.gl/react-google-maps";
import { MapPinOff } from "lucide-react";
import { useEffect, useMemo } from "react";
import type { LaneView, LatLon } from "../../../services/dispatchBoardApi";
import { EmptyState } from "../../ui";
import type { TruckPosition } from "./runningLate";

/** Same map style id as `MapView` (Advanced markers need a map id). */
const MAP_ID = "ff5c9f40e270515093c1c77f";

/** Per-lane colours, chosen for ≥ 3:1 contrast on the default road tiles. */
export const LANE_COLOURS = [
  "#1d4ed8",
  "#b91c1c",
  "#047857",
  "#7e22ce",
  "#c2410c",
  "#0f766e",
  "#a21caf",
  "#4d7c0f",
  "#1e40af",
  "#9f1239",
  "#334155",
  "#854d0e",
];

export function laneColour(index: number): { colour: string; dashed: boolean } {
  return {
    colour: LANE_COLOURS[index % LANE_COLOURS.length],
    dashed: index >= LANE_COLOURS.length,
  };
}

export interface MapStop {
  orderId: string;
  truckId: string;
  number: number;
  location: LatLon;
}

export interface LaneRouteMapProps {
  lanes: LaneView[];
  /** Only this load of the (single) lane; `null` = every load. */
  loadId?: string | null;
  selectedOrderIds: ReadonlySet<string>;
  onSelectStop: (orderId: string, truckId: string) => void;
  positions: Record<string, TruckPosition>;
  terminals: Record<string, LatLon>;
  /** Accessible name of the map region. */
  label: string;
  className?: string;
}

/** Stops with coordinates, numbered per lane in route order. */
export function mapStops(lane: LaneView, loadId?: string | null): MapStop[] {
  const out: MapStop[] = [];
  let n = 0;
  for (const load of lane.loads) {
    if (loadId && load.load_id !== loadId) continue;
    for (const stop of load.stops) {
      n += 1;
      if (stop.location) {
        out.push({
          orderId: stop.order_id,
          truckId: lane.truck_id,
          number: n,
          location: stop.location,
        });
      }
    }
  }
  return out;
}

function RoutePolyline({
  path,
  colour,
  dashed,
}: {
  path: LatLon[];
  colour: string;
  dashed: boolean;
}) {
  const map = useMap();
  useEffect(() => {
    if (!map || path.length < 2 || typeof google === "undefined") return;
    const line = new google.maps.Polyline({
      path: path.map((p) => ({ lat: p.lat, lng: p.lon })),
      strokeColor: colour,
      strokeOpacity: dashed ? 0 : 0.9,
      strokeWeight: 4,
      icons: dashed
        ? [
            {
              icon: { path: "M 0,-1 0,1", strokeOpacity: 1, scale: 3 },
              offset: "0",
              repeat: "14px",
            },
          ]
        : undefined,
      map,
    });
    return () => line.setMap(null);
  }, [map, path, colour, dashed]);
  return null;
}

function centreOf(points: LatLon[]): { lat: number; lng: number } {
  if (points.length === 0) return { lat: 39.5, lng: -98.35 };
  const lat = points.reduce((s, p) => s + p.lat, 0) / points.length;
  const lng = points.reduce((s, p) => s + p.lon, 0) / points.length;
  return { lat, lng };
}

export function LaneRouteMap({
  lanes,
  loadId = null,
  selectedOrderIds,
  onSelectStop,
  positions,
  terminals,
  label,
  className = "",
}: LaneRouteMapProps) {
  const apiKey = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY;
  const routes = useMemo(
    () =>
      lanes.map((lane, i) => {
        const stops = mapStops(lane, loadId);
        const loadTerminals = lane.loads
          .filter((l) => !loadId || l.load_id === loadId)
          .map((l) => (l.terminal_id ? terminals[l.terminal_id] : undefined))
          .filter((t): t is LatLon => Boolean(t));
        const start = loadTerminals[0];
        return {
          lane,
          stops,
          terminals: loadTerminals,
          path: [...(start ? [start] : []), ...stops.map((s) => s.location)],
          ...laneColour(i),
        };
      }),
    [lanes, loadId, terminals],
  );
  const allStops = routes.flatMap((r) => r.stops);

  if (!apiKey) {
    return (
      <section aria-label={label} className={className}>
        <EmptyState
          icon={<MapPinOff />}
          title="Map unavailable"
          description="Add a Google Maps API key to show routes on the board."
          className="py-8"
        />
      </section>
    );
  }

  const centre = centreOf([
    ...allStops.map((s) => s.location),
    ...routes.flatMap((r) => r.terminals),
  ]);

  return (
    <section
      aria-label={label}
      className={`flex min-h-0 flex-col ${className}`}
    >
      <div className="relative min-h-48 flex-1" data-testid="lane-route-map">
        <APIProvider apiKey={apiKey}>
          <GoogleMap
            mapId={MAP_ID}
            defaultCenter={centre}
            defaultZoom={9}
            gestureHandling="cooperative"
            disableDefaultUI
            style={{ width: "100%", height: "100%" }}
          >
            {routes.map((r) => (
              <RoutePolyline
                key={`line-${r.lane.truck_id}`}
                path={r.path}
                colour={r.colour}
                dashed={r.dashed}
              />
            ))}
            {routes.flatMap((r) =>
              r.terminals.map((t, i) => (
                <AdvancedMarker
                  key={`terminal-${r.lane.truck_id}-${i}`}
                  position={{ lat: t.lat, lng: t.lon }}
                  title={`Terminal for Truck ${r.lane.truck_id}`}
                >
                  <div className="rounded bg-gray-900 px-1 text-[10px] font-semibold text-white">
                    T
                  </div>
                </AdvancedMarker>
              )),
            )}
            {routes.flatMap((r) =>
              r.stops.map((s) => {
                const selected = selectedOrderIds.has(s.orderId);
                return (
                  <AdvancedMarker
                    key={`stop-${s.orderId}`}
                    position={{ lat: s.location.lat, lng: s.location.lon }}
                    title={`Stop ${s.number}, Order ${s.orderId}, Truck ${s.truckId}`}
                    onClick={() => onSelectStop(s.orderId, s.truckId)}
                    zIndex={selected ? 10 : 1}
                  >
                    <div
                      data-selected={selected ? "true" : "false"}
                      className={`flex h-6 w-6 items-center justify-center rounded-full border-2 text-[11px] font-bold ${
                        selected
                          ? "border-white bg-gray-900 text-white"
                          : "border-white text-white"
                      }`}
                      style={selected ? undefined : { background: r.colour }}
                    >
                      {s.number}
                    </div>
                  </AdvancedMarker>
                );
              }),
            )}
            {routes.map((r) => {
              const pos = positions[r.lane.truck_id];
              if (!pos) return null;
              return (
                <AdvancedMarker
                  key={`truck-${r.lane.truck_id}`}
                  position={{ lat: pos.lat, lng: pos.lon }}
                  title={`Truck ${r.lane.truck_id}, live position`}
                  zIndex={20}
                >
                  <div
                    className="rounded-md border-2 border-white px-1 text-[10px] font-semibold text-white shadow"
                    style={{ background: r.colour }}
                  >
                    {r.lane.truck_id}
                  </div>
                </AdvancedMarker>
              );
            })}
          </GoogleMap>
        </APIProvider>
      </div>
      {allStops.length === 0 ? (
        <p className="px-3 py-2 text-xs text-gray-600">
          No stops with a location to show.
        </p>
      ) : (
        <ol
          aria-label="Stops on the map"
          className="flex max-h-28 flex-wrap gap-1 overflow-y-auto px-3 py-2"
        >
          {routes.flatMap((r) =>
            r.stops.map((s) => {
              const selected = selectedOrderIds.has(s.orderId);
              return (
                <li key={`item-${s.orderId}`}>
                  <button
                    type="button"
                    aria-pressed={selected}
                    onClick={() => onSelectStop(s.orderId, s.truckId)}
                    className={`min-h-6 rounded-md border px-1.5 text-xs outline-none focus-visible:ring-2 focus-visible:ring-primary ${
                      selected
                        ? "border-primary bg-primary-soft text-gray-900"
                        : "border-gray-300 text-gray-700 hover:bg-gray-50"
                    }`}
                  >
                    <span
                      aria-hidden="true"
                      className="mr-1 inline-block h-2 w-2 rounded-full"
                      style={{ background: r.colour }}
                    />
                    {lanes.length > 1 ? `Truck ${s.truckId}, ` : ""}Stop{" "}
                    {s.number}, Order {s.orderId}
                  </button>
                </li>
              );
            }),
          )}
        </ol>
      )}
    </section>
  );
}

export default LaneRouteMap;
