"use client";

import { AlertTriangle, BarChart3, Search } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { EfficiencyFilters, EfficiencyMetric } from "../../services/fuelApi";
import { getEfficiencyMetrics } from "../../services/fuelApi";

/**
 * Classify efficiency into a color tier.
 * - ≥ 4 km/L → good (green)
 * - ≥ 2 km/L → average (yellow)
 * - < 2 km/L → poor (red)
 */
function efficiencyTier(value: number): "good" | "average" | "poor" {
  if (value >= 4) return "good";
  if (value >= 2) return "average";
  return "poor";
}

const TIER_STYLES: Record<string, { text: string; bg: string; bar: string }> = {
  good:    { text: "text-emerald-700", bg: "bg-emerald-50", bar: "bg-emerald-500" },
  average: { text: "text-amber-700",   bg: "bg-amber-50",   bar: "bg-amber-500" },
  poor:    { text: "text-red-700",     bg: "bg-red-50",     bar: "bg-red-500" },
};

function formatNumber(n: number, decimals = 1): string {
  if (n == null || !Number.isFinite(n)) return "0";
  if (n >= 1_000) return `${(n / 1_000).toFixed(decimals)}K`;
  return n.toFixed(decimals);
}

/**
 * Fuel Efficiency Chart — displays per-asset fuel efficiency as a table
 * with inline bar visualisation and color-coded efficiency values.
 *
 * Manages its own data fetching, filters, loading, and error state.
 *
 * Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
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
        err instanceof Error ? err.message : "Failed to load efficiency metrics";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [assetFilter, startDate, endDate]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const maxEfficiency = data.length > 0
    ? Math.max(...data.map((m) => m.efficiency_km_per_liter ?? 0), 1)
    : 1;

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
            placeholder="Filter by asset ID..."
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
        <div className="flex items-center gap-2 px-4 py-3 bg-red-50 text-red-700 rounded-lg text-sm">
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
          <div className="w-5 h-5 border-2 border-gray-300 border-t-[#232323] rounded-full animate-spin mr-2" />
          Loading efficiency data...
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && data.length === 0 && (
        <div className="text-center py-8 text-gray-400 text-sm">
          <BarChart3 className="w-8 h-8 mx-auto mb-2 opacity-40" aria-hidden="true" />
          No efficiency data available for the selected filters
        </div>
      )}

      {/* Data table with inline bars */}
      {!loading && !error && data.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" aria-label="Fuel efficiency metrics">
            <thead>
              <tr className="border-b border-gray-200 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                <th className="px-3 py-2">Asset ID</th>
                <th className="px-3 py-2 text-right">Distance (km)</th>
                <th className="px-3 py-2 text-right">Fuel Consumed (L)</th>
                <th className="px-3 py-2 text-right">Efficiency (km/L)</th>
                <th className="px-3 py-2 w-40" aria-label="Efficiency bar" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {data.map((metric) => {
                const eff = metric.efficiency_km_per_liter ?? 0;
                const tier = efficiencyTier(eff);
                const styles = TIER_STYLES[tier];
                const barWidth = (eff / maxEfficiency) * 100;

                return (
                  <tr
                    key={metric.asset_id}
                    className="hover:bg-gray-50 transition-colors"
                  >
                    <td className="px-3 py-2.5 font-medium text-[#232323]">
                      {metric.asset_id}
                    </td>
                    <td className="px-3 py-2.5 text-right text-gray-600">
                      {formatNumber(metric.distance_km)}
                    </td>
                    <td className="px-3 py-2.5 text-right text-gray-600">
                      {formatNumber(metric.fuel_consumed_liters)}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${styles.text} ${styles.bg}`}
                      >
                        {eff.toFixed(2)}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="w-full bg-gray-100 rounded-full h-2">
                        <div
                          className={`h-2 rounded-full ${styles.bar} transition-all`}
                          style={{ width: `${Math.max(barWidth, 2)}%` }}
                          title={`${eff.toFixed(2)} km/L`}
                        />
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
