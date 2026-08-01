"use client";

import { TrendingDown, TrendingUp } from "lucide-react";
import { LITERS_PER_GALLON } from "../../services/fuelApi";
import type { ConsumptionMetric, FuelType } from "../../services/fuelApi";

interface FuelConsumptionChartProps {
  /** Daily consumption metrics, may include multiple fuel types */
  data: ConsumptionMetric[];
}

const FUEL_TYPE_COLORS: Record<FuelType, { bar: string; label: string }> = {
  DIESEL_2: { bar: "bg-info", label: "Diesel #2" },
  GASOLINE_REG: { bar: "bg-warning", label: "Regular Unleaded" },
  GASOLINE_PREM: { bar: "bg-error", label: "Premium Unleaded" },
  HEATING_OIL: { bar: "bg-warning", label: "Heating Oil" },
  PROPANE: { bar: "bg-success", label: "Propane" },
  KEROSENE: { bar: "bg-brand-secondary", label: "Kerosene" },
  OFF_ROAD_DIESEL: { bar: "bg-slate-500", label: "Off-Road Diesel" },
  DEF: { bar: "bg-info", label: "DEF" },
};

const DEFAULT_COLOR = { bar: "bg-gray-400", label: "Other" };

function fuelConfig(ft: string) {
  return FUEL_TYPE_COLORS[ft as FuelType] ?? DEFAULT_COLOR;
}

interface DayBucket {
  date: string;
  byFuelType: Record<string, number>;
  events: number;
  total: number;
}

function groupByDay(data: ConsumptionMetric[]): DayBucket[] {
  const map = new Map<
    string,
    { byFuelType: Record<string, number>; events: number }
  >();

  for (const metric of data) {
    const date = metric.timestamp.slice(0, 10); // YYYY-MM-DD
    let bucket = map.get(date);
    if (!bucket) {
      bucket = { byFuelType: {}, events: 0 };
      map.set(date, bucket);
    }
    const fuelType = metric.fuel_type ?? "Other";
    const gallons =
      metric.total_gallons ?? metric.total_liters / LITERS_PER_GALLON;
    bucket.byFuelType[fuelType] = (bucket.byFuelType[fuelType] ?? 0) + gallons;
    bucket.events += metric.event_count ?? 0;
  }

  return Array.from(map.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, { byFuelType, events }]) => ({
      date,
      byFuelType,
      events,
      total: Object.values(byFuelType).reduce((s, v) => s + v, 0),
    }));
}

function formatDateLabel(dateStr: string): string {
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) return dateStr;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function formatGallons(gallons: number): string {
  if (gallons >= 1_000) return `${(gallons / 1_000).toFixed(1)}K`;
  return Math.round(gallons).toLocaleString();
}

/** Average of the most-recent half vs the earlier half, as a % delta. */
function recentTrendPct(buckets: DayBucket[]): number | null {
  if (buckets.length < 4) return null;
  const mid = Math.floor(buckets.length / 2);
  const earlier = buckets.slice(0, mid);
  const recent = buckets.slice(mid);
  const avg = (xs: DayBucket[]) =>
    xs.reduce((s, b) => s + b.total, 0) / (xs.length || 1);
  const e = avg(earlier);
  const r = avg(recent);
  if (e <= 0) return null;
  return ((r - e) / e) * 100;
}

function Stat({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div>
      <p className="text-xs text-gray-500">{label}</p>
      <p className="text-lg font-semibold text-gray-900 leading-tight">
        {value}
      </p>
      {sub && <p className="text-[11px] text-gray-500">{sub}</p>}
    </div>
  );
}

/**
 * Daily consumption trend.
 *
 * Renders a compact insight header (period total, daily average, peak day,
 * and a recent-vs-earlier trend chip) above a responsive stacked-bar chart
 * with horizontal gridlines and a dashed daily-average reference line. Bars
 * stack by fuel type and reveal a per-day breakdown on hover. CSS/HTML based
 * (no external chart library), consistent with the project's chart patterns.
 *
 * Validates: Requirements 6.3
 */
export default function FuelConsumptionChart({
  data,
}: FuelConsumptionChartProps) {
  const buckets = groupByDay(data);

  if (buckets.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-gray-200 py-10 text-center text-sm text-gray-500">
        No consumption data for the selected time range
      </div>
    );
  }

  const maxTotal = Math.max(...buckets.map((b) => b.total), 1);
  const periodTotal = buckets.reduce((s, b) => s + b.total, 0);
  const dailyAvg = periodTotal / buckets.length;
  const peak = buckets.reduce(
    (m, b) => (b.total > m.total ? b : m),
    buckets[0],
  );
  const trend = recentTrendPct(buckets);

  // Fuel types present, ordered by total volume desc (largest at the base).
  const totalsByFuel = new Map<string, number>();
  for (const b of buckets) {
    for (const [ft, g] of Object.entries(b.byFuelType)) {
      totalsByFuel.set(ft, (totalsByFuel.get(ft) ?? 0) + g);
    }
  }
  const fuelTypes = Array.from(totalsByFuel.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([ft]) => ft);

  // Three gridlines: 0, half, max.
  const gridValues = [maxTotal, maxTotal / 2, 0];
  const avgPct = (dailyAvg / maxTotal) * 100;

  return (
    <div className="space-y-4">
      {/* Insight header */}
      <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
        <Stat
          label={`Total · ${buckets.length} day${buckets.length === 1 ? "" : "s"}`}
          value={`${formatGallons(periodTotal)} gal`}
        />
        <Stat label="Daily average" value={`${formatGallons(dailyAvg)} gal`} />
        <Stat
          label="Peak day"
          value={`${formatGallons(peak.total)} gal`}
          sub={formatDateLabel(peak.date)}
        />
        {trend !== null && (
          <div className="ml-auto self-center">
            <span
              className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium ${
                trend >= 0
                  ? "bg-warning-light text-warning-dark"
                  : "bg-success-light text-success-dark"
              }`}
              title="Recent half vs earlier half of the period"
            >
              {trend >= 0 ? (
                <TrendingUp className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <TrendingDown className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              {trend >= 0 ? "+" : ""}
              {trend.toFixed(0)}% vs earlier
            </span>
          </div>
        )}
      </div>

      {/* Legend */}
      <div className="flex flex-wrap gap-3" aria-label="Chart legend">
        {fuelTypes.map((ft) => {
          const cfg = fuelConfig(ft);
          return (
            <div
              key={ft}
              className="flex items-center gap-1.5 text-xs text-gray-600"
            >
              <span
                className={`h-3 w-3 rounded-sm ${cfg.bar}`}
                aria-hidden="true"
              />
              {cfg.label}
            </div>
          );
        })}
      </div>

      {/* Plot */}
      <div className="flex gap-2">
        {/* Y axis */}
        <div className="relative w-12 shrink-0" style={{ height: 192 }}>
          {gridValues.map((v) => (
            <span
              key={v}
              className="absolute right-0 -translate-y-1/2 text-[10px] tabular-nums text-gray-400"
              style={{ top: `${(1 - v / maxTotal) * 100}%` }}
            >
              {formatGallons(v)}
            </span>
          ))}
        </div>

        {/* Bars + gridlines */}
        <div className="min-w-0 flex-1">
          <div className="relative" style={{ height: 192 }}>
            {/* Gridlines */}
            {gridValues.map((v) => (
              <div
                key={v}
                className="absolute inset-x-0 border-t border-gray-100"
                style={{ top: `${(1 - v / maxTotal) * 100}%` }}
                aria-hidden="true"
              />
            ))}

            {/* Daily-average reference line */}
            <div
              className="absolute inset-x-0 border-t border-dashed border-primary/50"
              style={{ top: `${100 - avgPct}%` }}
              aria-hidden="true"
            >
              <span className="absolute right-0 -top-4 rounded bg-primary/10 px-1 text-[10px] font-medium text-primary">
                avg
              </span>
            </div>

            {/* Bars */}
            <div className="absolute inset-0 flex items-end gap-1">
              {buckets.map((bucket) => {
                const heightPct = (bucket.total / maxTotal) * 100;
                return (
                  <div
                    key={bucket.date}
                    className="group relative flex h-full flex-1 items-end"
                    style={{ minWidth: 6 }}
                  >
                    {/* Hover tooltip */}
                    <div className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-2 hidden -translate-x-1/2 whitespace-nowrap rounded-lg border border-gray-200 bg-white px-3 py-2 text-left shadow-lg group-hover:block">
                      <p className="mb-1 text-xs font-semibold text-gray-900">
                        {formatDateLabel(bucket.date)}
                      </p>
                      <p className="mb-1.5 text-[11px] text-gray-500">
                        {formatGallons(bucket.total)} gal ·{" "}
                        {bucket.events.toLocaleString()} event
                        {bucket.events === 1 ? "" : "s"}
                      </p>
                      <div className="space-y-0.5">
                        {fuelTypes
                          .filter((ft) => (bucket.byFuelType[ft] ?? 0) > 0)
                          .map((ft) => {
                            const cfg = fuelConfig(ft);
                            return (
                              <div
                                key={ft}
                                className="flex items-center gap-1.5 text-[11px] text-gray-600"
                              >
                                <span
                                  className={`h-2 w-2 rounded-sm ${cfg.bar}`}
                                  aria-hidden="true"
                                />
                                <span className="flex-1">{cfg.label}</span>
                                <span className="tabular-nums text-gray-900">
                                  {formatGallons(bucket.byFuelType[ft])}
                                </span>
                              </div>
                            );
                          })}
                      </div>
                    </div>

                    {/* Stacked bar */}
                    <div
                      className="flex w-full flex-col-reverse overflow-hidden rounded-t-md ring-1 ring-transparent transition-all group-hover:ring-gray-300"
                      style={{
                        height: `${Math.max(heightPct, bucket.total > 0 ? 2 : 0)}%`,
                      }}
                    >
                      {fuelTypes.map((ft) => {
                        const gallons = bucket.byFuelType[ft] ?? 0;
                        if (gallons <= 0) return null;
                        const segPct = (gallons / bucket.total) * 100;
                        const cfg = fuelConfig(ft);
                        return (
                          <div
                            key={ft}
                            className={`w-full ${cfg.bar}`}
                            style={{ height: `${segPct}%` }}
                          />
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* X axis labels — share the bar layout so they stay aligned */}
          <div className="mt-1.5 flex gap-1">
            {buckets.map((bucket, i) => {
              // Thin to ~8 labels max to avoid crowding on long ranges.
              const step = Math.ceil(buckets.length / 8);
              const show = i % step === 0 || i === buckets.length - 1;
              return (
                <div
                  key={`${bucket.date}-label`}
                  className="min-w-0 flex-1 text-center"
                  style={{ minWidth: 6 }}
                >
                  {show && (
                    <span className="block truncate text-[10px] text-gray-500">
                      {formatDateLabel(bucket.date)}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <p className="sr-only">
        Daily fuel consumption over {buckets.length} days. Period total{" "}
        {formatGallons(periodTotal)} gallons, daily average{" "}
        {formatGallons(dailyAvg)} gallons, peak {formatGallons(peak.total)}{" "}
        gallons on {formatDateLabel(peak.date)}.
      </p>
    </div>
  );
}
