"use client";

import { MapPin } from "lucide-react";
import type { JobStatus } from "../../types/api";

interface AssetLocation {
  asset_id: string;
  name: string;
  lat: number;
  lng: number;
  job_status?: JobStatus;
  job_id?: string;
}

interface OperationsMapProps {
  assets: AssetLocation[];
}

/** Color-coded markers by job status */
const STATUS_COLORS: Record<
  JobStatus | "unassigned",
  { bg: string; text: string; label: string }
> = {
  scheduled: {
    bg: "bg-info-light",
    text: "text-info-dark",
    label: "Scheduled",
  },
  assigned: {
    bg: "bg-warning-light",
    text: "text-warning-dark",
    label: "Assigned",
  },
  in_progress: {
    bg: "bg-success-light",
    text: "text-success-dark",
    label: "In Progress",
  },
  completed: { bg: "bg-gray-100", text: "text-gray-600", label: "Completed" },
  cancelled: { bg: "bg-gray-100", text: "text-gray-500", label: "Cancelled" },
  failed: { bg: "bg-error-light", text: "text-error-dark", label: "Failed" },
  unassigned: { bg: "bg-gray-50", text: "text-gray-400", label: "Unassigned" },
};

/**
 * Map overlay showing asset locations with job assignment indicators,
 * color-coded by job status. Uses a simplified placeholder map view.
 *
 * Validates: Requirement 10.3
 */
export default function OperationsMap({ assets }: OperationsMapProps) {
  return (
    <div className="h-full flex flex-col bg-white rounded-xl border border-gray-100">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <MapPin className="w-4 h-4 text-primary" />
          <h3 className="text-sm font-semibold text-primary">
            Asset Locations
          </h3>
        </div>
        <span className="text-xs text-gray-400">{assets.length} assets</span>
      </div>

      {/* Legend */}
      <div className="flex flex-wrap gap-2 px-4 py-2 border-b border-gray-50">
        {(["in_progress", "assigned", "scheduled", "unassigned"] as const).map(
          (status) => {
            const config = STATUS_COLORS[status];
            return (
              <span
                key={status}
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${config.bg} ${config.text}`}
              >
                <span className="w-2 h-2 rounded-full bg-current" />
                {config.label}
              </span>
            );
          },
        )}
      </div>

      {/* Map placeholder with asset markers */}
      <div className="flex-1 relative bg-gray-50 overflow-auto p-4">
        {assets.length === 0 ? (
          <div className="h-full flex items-center justify-center text-gray-400 text-sm">
            No assets with location data
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            {assets.map((asset) => {
              const status = asset.job_status ?? "unassigned";
              const config = STATUS_COLORS[status] ?? STATUS_COLORS.unassigned;
              return (
                <div
                  key={asset.asset_id}
                  className={`${config.bg} rounded-lg px-3 py-2 border border-gray-100`}
                >
                  <div className="flex items-center gap-1.5">
                    <span
                      className={`w-2 h-2 rounded-full ${config.text} bg-current`}
                    />
                    <span className="text-xs font-medium text-primary truncate">
                      {asset.name}
                    </span>
                  </div>
                  {asset.job_id && (
                    <p className="text-xs text-gray-500 mt-0.5 truncate">
                      {asset.job_id}
                    </p>
                  )}
                  <p className="text-xs text-gray-400 mt-0.5">
                    {asset.lat.toFixed(4)}, {asset.lng.toFixed(4)}
                  </p>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
