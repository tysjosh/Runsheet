"use client";

import type { ConsumptionMetric, FuelType } from "../../services/fuelApi";

interface FuelConsumptionChartProps {
  /** Daily consumption metrics, may include multiple fuel types */
  data: ConsumptionMetric[];
}

const FUEL_TYPE_COLORS: Record<FuelType, { bar: string; label: string }> = {
  AGO: { bar: "bg-blue-500",   label: "AGO (Diesel)" },
  PMS: { bar: "bg-amber-500",  label: "PMS (Petrol)" },
  ATK: { bar: "bg-purple-500", label: "ATK (Aviation)" },
  LPG: { bar: "bg-emerald-500", label: "LPG (Gas)" },
};

const DEFAULT_COLOR = { bar: "bg-gray-400", label: "Other" };

interface DayBucket {
  date: string;
  byFuelType: Record<string, number>;
  total: number;
}

function groupByDay(data: ConsumptionMetric[]): DayBucket[] {
  const map = new Map<string, Record<string, number>>();

  for (const metric of data) {
    const date = metric.timestamp.slice(0, 10); // YYYY-MM-DD
    if (!map.has(date)) map.set(date, {});
    const bucket = map.get(date)!;
    const fuelType = metric.fuel_type ?? "Other";
    bucket[fuelType] = (bucket[fuelType] ?? 0) + metric.total_liters;
  }

  return Array.from(map.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, byFuelType]) => ({
      date,
      byFuelType,
      total: Object.values(byFuelType).reduce((s, v) => s + v, 0),
    }));
}

function formatDateLabel(dateStr: string): string {
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) return dateStr;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function formatLiters(liters: number): string {
  if (liters >= 1_000) return `${(liters / 1_000).toFixed(1)}K`;
  return liters.toFixed(0);
}

/**
 * Daily consumption trend chart with stacked bars per fuel type.
 * Uses CSS/HTML-based bars (no external chart library), consistent
 * with the existing chart patterns in the project.
 *
 * Validates: Requirements 6.3
 */
export default function FuelConsumptionChart({ data }: FuelConsumptionChartProps) {
  const buckets = groupByDay(data);

  if (buckets.length === 0) {
    return (
      <div className="text-center py-8 text-gray-400 text-sm">
        No consumption data for the selected time range
      </div>
    );
  }

  const maxTotal = Math.max(...buckets.map((b) => b.total), 1);

  // Collect all fuel types present in the data for the legend
  const fuelTypesInData = new Set<string>();
  for (const bucket of buckets) {
    for (const ft of Object.keys(bucket.byFuelType)) {
      fuelTypesInData.add(ft);
    }
  }
  const fuelTypes = Array.from(fuelTypesInData).sort();

  return (
    <div className="space-y-3">
      {/* Legend */}
      <div className="flex flex-wrap gap-3" aria-label="Chart legend">
        {fuelTypes.map((ft) => {
          const cfg = FUEL_TYPE_COLORS[ft as FuelType] ?? DEFAULT_COLOR;
          return (
            <div key={ft} className="flex items-center gap-1.5 text-xs text-gray-600">
              <span className={`w-3 h-3 rounded-sm ${cfg.bar}`} aria-hidden="true" />
              {cfg.label}
            </div>
          );
        })}
      </div>

      {/* Y-axis max label */}
      <div className="flex items-end gap-2 text-xs text-gray-400">
        <span className="w-12 text-right">{formatLiters(maxTotal)}</span>
        <div className="flex-1 border-b border-dashed border-gray-200" />
      </div>

      {/* Stacked bars */}
      <div
        className="flex items-end gap-1 overflow-x-auto pb-1"
        style={{ minHeight: 140 }}
        role="img"
        aria-label="Daily fuel consumption chart"
      >
        {buckets.map((bucket) => {
          const heightPct = (bucket.total / maxTotal) * 100;
          return (
            <div
              key={bucket.date}
              className="flex flex-col items-center flex-1 min-w-[28px] max-w-[56px]"
            >
              <span className="text-[10px] text-gray-500 mb-1">
                {bucket.total > 0 ? formatLiters(bucket.total) : ""}
              </span>
              <div
                className="w-full flex flex-col-reverse rounded-t overflow-hidden"
                style={{
                  height: `${Math.max(heightPct, bucket.total > 0 ? 4 : 0)}px`,
                  maxHeight: 120,
                }}
                title={`${formatDateLabel(bucket.date)}: ${formatLiters(bucket.total)} L total`}
              >
                {fuelTypes.map((ft) => {
                  const liters = bucket.byFuelType[ft] ?? 0;
                  if (liters <= 0) return null;
                  const segPct = (liters / bucket.total) * 100;
                  const cfg = FUEL_TYPE_COLORS[ft as FuelType] ?? DEFAULT_COLOR;
                  return (
                    <div
                      key={ft}
                      className={`w-full ${cfg.bar} transition-all`}
                      style={{ height: `${segPct}%` }}
                      title={`${cfg.label}: ${formatLiters(liters)} L`}
                    />
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>

      {/* X-axis labels */}
      <div className="flex gap-1 overflow-x-auto">
        {buckets.map((bucket) => (
          <div
            key={`${bucket.date}-label`}
            className="flex-1 min-w-[28px] max-w-[56px] text-center"
          >
            <span className="text-[10px] text-gray-400 truncate block">
              {formatDateLabel(bucket.date)}
            </span>
          </div>
        ))}
      </div>

      {/* Zero line */}
      <div className="flex items-center gap-2 text-xs text-gray-400">
        <span className="w-12 text-right">0</span>
        <div className="flex-1 border-b border-gray-200" />
      </div>
    </div>
  );
}
