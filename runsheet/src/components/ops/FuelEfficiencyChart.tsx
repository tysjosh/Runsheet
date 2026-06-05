"use client";

import { AlertTriangle, BarChart3, Search } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { type Column, Table } from "@/components/ui";
import type {
  EfficiencyFilters,
  EfficiencyMetric,
} from "../../services/fuelApi";
import {
  efficiencyKmPerLiter,
  getEfficiencyMetrics,
} from "../../services/fuelApi";

/**
 * Classify km/L efficiency into a colour tier (higher km/L is better).
 * - ≥ 4 km/L → good (green)
 * - ≥ 2 km/L → average (yellow)
 * - < 2 km/L → poor (red)
 */
function efficiencyTier(kmPerLiter: number): "good" | "average" | "poor" {
  if (kmPerLiter >= 4) return "good";
  if (kmPerLiter >= 2) return "average";
  return "poor";
}

const TIER_STYLES: Record<string, { text: string; bg: string; bar: string }> = {
  good: {
    text: "text-success-dark",
    bg: "bg-success-light",
    bar: "bg-success",
  },
  average: {
    text: "text-warning-dark",
    bg: "bg-warning-light",
    bar: "bg-warning",
  },
  poor: {
    text: "text-error-dark",
    bg: "bg-error-light",
    bar: "bg-error",
  },
};

function formatNumber(n: number | null | undefined, decimals = 1): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n >= 1_000) return `${(n / 1_000).toFixed(decimals)}K`;
  return n.toFixed(decimals);
}

/**
 * Fleet Fuel Efficiency chart — per-vehicle fuel economy.
 *
 * This is a FLEET performance metric, not a storage-tank metric: it
 * reads ``GET /fuel/metrics/efficiency``, which aggregates consumption
 * events by ``asset_id`` (the truck/vehicle that received the fuel) and
 * derives distance from the min→max ``odometer_reading`` recorded in the
 * window. The backend reports ``liters_per_km`` (lower is better); this
 * component converts to km/L for display (higher is better) and renders a
 * colour-coded table with inline bars.
 *
 * Vehicles whose consumption events lack odometer readings have no
 * derivable efficiency — they render an explicit "No odometer data" state
 * rather than a misleading 0.
 *
 * Validates: Requirements 5.2, 5.3 (fuel-monitoring).
 */
export default function FuelEfficiencyChart() {
  const [data, setData] = useState<EfficiencyMetric[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filter state
  const [assetFilter, setAssetFilter] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      const filters: EfficiencyFilters = {};
      if (assetFilter.trim()) filters.asset_id = assetFilter.trim();
      if (startDate) filters.start_date = startDate;
      if (endDate) filters.end_date = endDate;

      const res = await getEfficiencyMetrics(filters);
      setData(res.data);
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to load efficiency metrics";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [assetFilter, startDate, endDate]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Scale bars against the best km/L in the current result set. Rows with
  // no derivable efficiency (null) contribute 0 to the max.
  const maxEfficiency =
    data.length > 0
      ? Math.max(...data.map((m) => efficiencyKmPerLiter(m) ?? 0), 1)
      : 1;

  const efficiencyColumns: Column<EfficiencyMetric>[] = [
    {
      key: "asset_id",
      label: "Vehicle / Asset",
      className: "font-medium text-primary",
      render: (metric) => metric.asset_id,
    },
    {
      key: "total_distance_km",
      label: "Distance (km)",
      align: "right",
      className: "text-gray-600",
      render: (metric) => formatNumber(metric.total_distance_km),
    },
    {
      key: "total_liters",
      label: "Fuel Consumed (L)",
      align: "right",
      className: "text-gray-600",
      render: (metric) => formatNumber(metric.total_liters),
    },
    {
      key: "efficiency",
      label: "Efficiency (km/L)",
      align: "right",
      render: (metric) => {
        const kmPerLiter = efficiencyKmPerLiter(metric);
        if (kmPerLiter == null) {
          return (
            <span
              className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium text-gray-500 bg-gray-100"
              title="No odometer readings recorded for this vehicle in the selected window, so efficiency cannot be derived."
            >
              No odometer data
            </span>
          );
        }
        const styles = TIER_STYLES[efficiencyTier(kmPerLiter)];
        return (
          <span
            className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${styles.text} ${styles.bg}`}
          >
            {kmPerLiter.toFixed(2)}
          </span>
        );
      },
    },
    {
      key: "bar",
      label: "",
      headerClassName: "w-40",
      render: (metric) => {
        const kmPerLiter = efficiencyKmPerLiter(metric);
        const hasEfficiency = kmPerLiter != null;
        const styles =
          TIER_STYLES[hasEfficiency ? efficiencyTier(kmPerLiter) : "average"];
        const barWidth = hasEfficiency ? (kmPerLiter / maxEfficiency) * 100 : 0;
        return (
          <div className="w-full bg-gray-100 rounded-full h-2">
            <div
              className={`h-2 rounded-full ${styles.bar} transition-all`}
              style={{ width: `${Math.max(barWidth, 2)}%` }}
              title={
                hasEfficiency
                  ? `${kmPerLiter.toFixed(2)} km/L`
                  : "No efficiency data"
              }
            />
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-wrap items-end gap-3">
        <div className="relative">
          <Search
            className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
            aria-hidden="true"
          />
          <input
            type="text"
            value={assetFilter}
            onChange={(e) => setAssetFilter(e.target.value)}
            placeholder="Filter by vehicle/asset ID..."
            className="pl-8 pr-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            aria-label="Filter by asset ID"
          />
        </div>

        <div className="flex items-center gap-2">
          <label htmlFor="eff-start-date" className="text-xs text-gray-500">
            From
          </label>
          <input
            id="eff-start-date"
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            aria-label="Start date"
          />
        </div>

        <div className="flex items-center gap-2">
          <label htmlFor="eff-end-date" className="text-xs text-gray-500">
            To
          </label>
          <input
            id="eff-end-date"
            type="date"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
            className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            aria-label="End date"
          />
        </div>
      </div>

      {/* Error state */}
      {error && (
        <div className="flex items-center gap-2 px-4 py-3 bg-error-light text-error-dark rounded-lg text-sm">
          <AlertTriangle className="w-4 h-4 flex-shrink-0" aria-hidden="true" />
          <span>{error}</span>
          <button
            type="button"
            onClick={loadData}
            className="ml-auto text-xs font-medium underline hover:no-underline"
          >
            Retry
          </button>
        </div>
      )}

      {/* Loading state */}
      {loading && !error && (
        <div className="flex items-center justify-center py-8 text-gray-400 text-sm">
          <div className="w-5 h-5 border-2 border-gray-300 border-t-primary rounded-full animate-spin mr-2" />
          Loading efficiency data...
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && data.length === 0 && (
        <div className="text-center py-8 text-gray-400 text-sm">
          <BarChart3
            className="w-8 h-8 mx-auto mb-2 opacity-40"
            aria-hidden="true"
          />
          No efficiency data available for the selected filters
        </div>
      )}

      {/* Data table with inline bars */}
      {!loading && !error && data.length > 0 && (
        <Table<EfficiencyMetric>
          ariaLabel="Fleet fuel efficiency metrics"
          variant="compact"
          columns={efficiencyColumns}
          data={data}
          getRowId={(metric) => metric.asset_id}
        />
      )}
    </div>
  );
}
