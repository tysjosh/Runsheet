"use client";

/**
 * Live map of all trucks (UI revamp R9.1, R9.3, §7.3).
 *
 * Each truck is a marker in its stable identity colour (the same colour as its
 * board lane and Dashboard run) with its status. The map is never the only
 * way in: the same trucks are listed beside it with an identity avatar, a
 * `StatusBadge` (icon + label) and the job, so the information is reachable
 * by keyboard and screen reader and when no Google Maps key is configured.
 * The raw latitude/longitude grid is gone.
 */
import {
  AdvancedMarker,
  APIProvider,
  Map as GoogleMap,
} from "@vis.gl/react-google-maps";
import { MapPinOff } from "lucide-react";
import Link from "next/link";
import { identityFor } from "../../lib/identity";
import type { JobStatus } from "../../types/api";
import { IdentityAvatar, StatusBadge, statusKeyFor } from "../ui";

export interface AssetLocation {
  asset_id: string;
  name: string;
  lat: number;
  lng: number;
  job_status?: JobStatus;
  job_id?: string;
  delayed?: boolean;
}

interface OperationsMapProps {
  assets: AssetLocation[];
  className?: string;
}

/** Same map style id as `MapView` (Advanced markers need a map id). */
const MAP_ID = "ff5c9f40e270515093c1c77f";

const STATUS_LABEL: Partial<Record<JobStatus, string>> = {
  scheduled: "Scheduled",
  assigned: "Assigned",
  in_progress: "In transit",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function assetStatus(a: AssetLocation) {
  if (a.delayed) return { key: "delayed" as const, label: "Delayed" };
  if (!a.job_status) return { key: "draft" as const, label: "Idle" };
  return {
    key:
      a.job_status === "scheduled"
        ? ("planned" as const)
        : (statusKeyFor(a.job_status) ?? "draft"),
    label: STATUS_LABEL[a.job_status] ?? a.job_status,
  };
}

function center(assets: AssetLocation[]) {
  if (assets.length === 0) return { lat: 39.5, lng: -98.35 };
  return {
    lat: assets.reduce((s, a) => s + a.lat, 0) / assets.length,
    lng: assets.reduce((s, a) => s + a.lng, 0) / assets.length,
  };
}

export default function OperationsMap({
  assets,
  className = "",
}: OperationsMapProps) {
  const apiKey = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY;
  return (
    <section
      aria-labelledby="live-map-title"
      className={`flex min-h-0 flex-col overflow-hidden rounded-[10px] border border-slate-200 bg-surface ${className}`}
    >
      <div className="flex h-10 shrink-0 items-center gap-2 border-b border-slate-200 px-3">
        <span aria-hidden="true" className="h-4 w-1 rounded-sm bg-cyan-700" />
        <h2
          id="live-map-title"
          className="text-[13px] font-semibold text-slate-900"
        >
          Trucks
        </h2>
        <span className="text-xs text-text-muted">
          {assets.length} with a position
        </span>
      </div>
      <div className="relative min-h-[220px] flex-1 bg-slate-100">
        {!apiKey ? (
          <div className="flex h-full flex-col items-center justify-center gap-1.5 p-4 text-center text-sm text-text-muted">
            <MapPinOff aria-hidden="true" className="h-6 w-6" />
            Map unavailable: no Google Maps key is configured. The trucks are
            listed below.
          </div>
        ) : (
          <APIProvider apiKey={apiKey}>
            <GoogleMap
              mapId={MAP_ID}
              defaultCenter={center(assets)}
              defaultZoom={assets.length > 0 ? 9 : 4}
              gestureHandling="cooperative"
              disableDefaultUI
              zoomControl
              className="h-full w-full"
            >
              {assets.map((a) => {
                const color = identityFor(a.asset_id).hex;
                const s = assetStatus(a);
                return (
                  <AdvancedMarker
                    key={a.asset_id}
                    position={{ lat: a.lat, lng: a.lng }}
                    title={`${a.name}: ${s.label}`}
                  >
                    <span
                      className="flex items-center gap-1 rounded-full border-2 border-white px-1.5 py-0.5 text-[11px] font-bold text-white shadow"
                      style={{ backgroundColor: color }}
                    >
                      {a.name}
                    </span>
                  </AdvancedMarker>
                );
              })}
            </GoogleMap>
          </APIProvider>
        )}
      </div>
      {assets.length > 0 && (
        <ul
          aria-label="Trucks on the map"
          className="max-h-48 shrink-0 overflow-y-auto border-t border-slate-200"
        >
          {assets.map((a) => {
            const s = assetStatus(a);
            return (
              <li
                key={a.asset_id}
                className="flex h-10 items-center gap-2 border-b border-slate-100 px-3 text-sm last:border-b-0"
              >
                <IdentityAvatar id={a.asset_id} label={a.name} size="xs" />
                <span className="min-w-0 flex-1 truncate font-semibold text-slate-900">
                  {a.name}
                </span>
                {a.job_id && (
                  <Link
                    href={`/dashboard/dispatch/jobs/${encodeURIComponent(a.job_id)}`}
                    className="truncate text-xs text-link hover:underline"
                  >
                    {a.job_id}
                  </Link>
                )}
                <StatusBadge status={s.key} label={s.label} />
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
