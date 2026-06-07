"use client";

import { MapPin, Navigation } from "lucide-react";
import { useMemo } from "react";
import type { OpsEvent } from "../../services/opsApi";

interface ShipmentMapProps {
  events: OpsEvent[];
}

interface RoutePoint {
  id: string;
  label: string;
  lat: number;
  lon: number;
  x: number;
  y: number;
}

function formatEventType(value: string): string {
  return value.replace(/_/g, " ");
}

function buildRoutePoints(events: OpsEvent[]): RoutePoint[] {
  const locatedEvents = events.filter((event) => event.location);

  if (locatedEvents.length === 0) return [];

  const lats = locatedEvents.map((event) => event.location?.lat ?? 0);
  const lons = locatedEvents.map((event) => event.location?.lon ?? 0);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const minLon = Math.min(...lons);
  const maxLon = Math.max(...lons);
  const latRange = maxLat - minLat || 1;
  const lonRange = maxLon - minLon || 1;

  return locatedEvents.map((event) => {
    const lat = event.location?.lat ?? 0;
    const lon = event.location?.lon ?? 0;
    const x = 8 + ((lon - minLon) / lonRange) * 84;
    const y = 92 - ((lat - minLat) / latRange) * 84;

    return {
      id: event.event_id,
      label: formatEventType(event.event_type),
      lat,
      lon,
      x,
      y,
    };
  });
}

export default function ShipmentMap({ events }: ShipmentMapProps) {
  const points = useMemo(() => buildRoutePoints(events), [events]);
  const polyline = points.map((point) => `${point.x},${point.y}`).join(" ");

  if (points.length === 0) {
    return (
      <div className="rounded-xl border border-gray-100 bg-gray-50 px-4 py-8 text-center">
        <MapPin className="mx-auto h-6 w-6 text-gray-500" aria-hidden="true" />
        <p className="mt-2 text-sm font-medium text-primary">
          No location data
        </p>
        <p className="mt-1 text-xs text-gray-500">
          Location pings will draw the route map here.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-gray-100 bg-gray-50">
      <div className="relative aspect-[4/3] bg-[linear-gradient(var(--color-gray-200)_1px,transparent_1px),linear-gradient(90deg,var(--color-gray-200)_1px,transparent_1px)] bg-[length:32px_32px]">
        <svg
          className="absolute inset-0 h-full w-full"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          {points.length > 1 && (
            <polyline
              points={polyline}
              fill="none"
              stroke="var(--color-primary)"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          )}
        </svg>

        {points.map((point, index) => {
          const isLatest = index === points.length - 1;

          return (
            <div
              key={point.id}
              className="absolute -translate-x-1/2 -translate-y-1/2"
              style={{ left: `${point.x}%`, top: `${point.y}%` }}
              title={`${point.label}: ${point.lat.toFixed(4)}, ${point.lon.toFixed(4)}`}
            >
              <span
                className={`flex h-7 w-7 items-center justify-center rounded-full border-2 border-white shadow-sm ${
                  isLatest
                    ? "bg-primary text-white"
                    : "bg-info-light text-info-dark"
                }`}
              >
                {isLatest ? (
                  <Navigation className="h-3.5 w-3.5" aria-hidden="true" />
                ) : (
                  <MapPin className="h-3.5 w-3.5" aria-hidden="true" />
                )}
              </span>
            </div>
          );
        })}
      </div>

      <div className="border-t border-gray-100 bg-white px-4 py-3">
        <p className="text-xs font-medium text-primary">
          {points.length} location {points.length === 1 ? "ping" : "pings"}
        </p>
        <p className="mt-1 text-xs text-gray-500 capitalize">
          Latest: {points[points.length - 1]?.label}
        </p>
      </div>
    </div>
  );
}
