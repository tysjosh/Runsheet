import { Activity } from "lucide-react";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { usePageChrome } from "@/components/ui";
import { CHART, COLOR, SEMANTIC } from "@/styles/tokens";
import { number } from "../lib/format";
import {
  type AnalyticsMetricKey,
  type AnalyticsMetrics,
  type AnalyticsTimeSeriesPoint,
  apiService,
  type RoutePerformanceEntry,
} from "../services/api";
import LoadingSpinner from "./LoadingSpinner";

// Google Charts component
declare global {
  interface Window {
    google: any;
  }
}

interface GoogleChartProps {
  chartType: string;
  data: any[];
  options: any;
  width?: string;
  height?: string;
}

/** KPI cards in a fixed order. Absent keys are skipped. */
const METRIC_KEYS: AnalyticsMetricKey[] = [
  "delivery_performance",
  "average_delay",
  "fleet_utilization",
];

const NOT_AVAILABLE = "Not available";

/** Trend series window, matching the backend default. */
const TREND_RANGE = "30d" as const;

function getMetricLabel(metric: string) {
  const labels: Record<string, string> = {
    delivery_performance: "Performance (%)",
    average_delay: "Delay (minutes)",
    fleet_utilization: "Utilization (%)",
  };
  return labels[metric] || "Value";
}

/** Null-safe numeric parse of a display value like "80.0%" (F2). */
export function parseMetricValue(
  value: string | null | undefined,
): number | null {
  if (value == null) return null;
  const numeric = Number.parseFloat(value.replace(/[^0-9.-]/g, ""));
  return Number.isFinite(numeric) ? numeric : null;
}

/** Bucket timestamp → "YYYY-MM-DD" in UTC (snapshots are per UTC date). */
function utcDay(timestamp: string): string {
  const d = new Date(timestamp);
  return Number.isNaN(d.getTime()) ? timestamp : d.toISOString().slice(0, 10);
}

function formatAsOf(asOf: string): string {
  const d = new Date(asOf);
  return Number.isNaN(d.getTime())
    ? asOf
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function GoogleChart({
  chartType,
  data,
  options,
  width = "100%",
  height = "300px",
}: GoogleChartProps) {
  const chartRef = React.useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(false);

  // Load Google Charts script once
  useEffect(() => {
    if (typeof window === "undefined") return;

    // Already fully loaded
    if (window.google?.visualization?.arrayToDataTable) {
      setReady(true);
      return;
    }

    // Script already injected but not finished loading — wait for it
    if (window.google?.charts) {
      window.google.charts.setOnLoadCallback(() => setReady(true));
      return;
    }

    // First load — inject script
    const script = document.createElement("script");
    script.src = "https://www.gstatic.com/charts/loader.js";
    script.onload = () => {
      window.google.charts.load("current", {
        packages: ["corechart", "gauge"],
      });
      window.google.charts.setOnLoadCallback(() => setReady(true));
    };
    document.head.appendChild(script);
  }, []);

  // Draw chart when ready or data changes
  useEffect(() => {
    if (!ready || !chartRef.current || !window.google?.visualization) return;
    if (!data || data.length < 2) return; // Need header + at least one data row
    try {
      const dataTable = window.google.visualization.arrayToDataTable(data);
      const chart = new window.google.visualization[chartType](
        chartRef.current,
      );
      chart.draw(dataTable, options);
    } catch (e) {
      console.error("Failed to draw chart:", e);
    }
  }, [ready, data, chartType, options]);

  if (!ready) {
    return (
      <div
        style={{ width, height }}
        className="flex items-center justify-center text-gray-500 text-sm"
      >
        Loading chart...
      </div>
    );
  }

  return <div ref={chartRef} style={{ width, height }} />;
}

export default function Analytics() {
  const [selectedMetric, setSelectedMetric] = useState<AnalyticsMetricKey>(
    "delivery_performance",
  );
  const [metrics, setMetrics] = useState<AnalyticsMetrics | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [routePerformance, setRoutePerformance] = useState<
    RoutePerformanceEntry[]
  >([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [series, setSeries] = useState<AnalyticsTimeSeriesPoint[] | null>(null);

  const loadAnalyticsData = useCallback(async () => {
    try {
      setLoading(true);
      setLoadError(false);
      const [metricsResponse, routesResponse] = await Promise.all([
        apiService.getAnalyticsMetrics(),
        apiService.getAnalyticsRoutePerformance(),
      ]);

      setMetrics(metricsResponse.data ?? null);
      setAsOf(metricsResponse.as_of ?? null);
      setRoutePerformance(routesResponse.data ?? []);
    } catch (error) {
      // F13: an outage must not look like "no data".
      console.error("Failed to load analytics data:", error);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  // Loads once on mount. There is no time-range selector: the KPIs are one
  // daily snapshot (OI-50).
  useEffect(() => {
    loadAnalyticsData();
  }, [loadAnalyticsData]);

  // Real trend for the selected KPI (F3), re-fetched when it changes.
  useEffect(() => {
    if (!metrics) return;
    let cancelled = false;
    setSeries(null);
    apiService
      .getAnalyticsTimeSeries(selectedMetric, TREND_RANGE)
      .then((response) => {
        if (!cancelled) setSeries(response.data ?? []);
      })
      .catch((error) => {
        console.error("Failed to load analytics trend:", error);
        if (!cancelled) setSeries([]);
      });
    return () => {
      cancelled = true;
    };
  }, [metrics, selectedMetric]);

  const metricEntries = useMemo(
    () =>
      METRIC_KEYS.flatMap((key) => {
        const metric = metrics?.[key];
        return metric ? [[key, metric] as const] : [];
      }),
    [metrics],
  );

  const chartData = useMemo(
    () => ({
      timeSeriesData: [
        [
          { type: "string", label: "Date" },
          { type: "number", label: getMetricLabel(selectedMetric) },
        ],
        ...(series ?? []).map((point) => [
          utcDay(point.timestamp),
          point.value,
        ]),
      ],
      pieChartData: [
        ["Route", "Performance"],
        ...routePerformance.map((route) => [
          String(route.name),
          Number(route.performance) || 0,
        ]),
      ],
      barChartData: [
        [
          { type: "string", label: "Route" },
          { type: "number", label: "Performance" },
        ],
        ...routePerformance.map((route) => [
          String(route.name),
          Number(route.performance) || 0,
        ]),
      ],
    }),
    [routePerformance, selectedMetric, series],
  );

  const hasTrendPoint = (series ?? []).some((point) => point.value != null);

  const sortedRoutes = useMemo(
    () => [...routePerformance].sort((a, b) => b.performance - a.performance),
    [routePerformance],
  );

  // Weighted by orders scored when the backend reports it (F8).
  const averageRoutePerformance = useMemo(() => {
    if (routePerformance.length === 0) return null;
    const weightOf = (r: RoutePerformanceEntry) =>
      r.orders_scored && r.orders_scored > 0 ? r.orders_scored : 1;
    const totalWeight = routePerformance.reduce((s, r) => s + weightOf(r), 0);
    const weighted = routePerformance.reduce(
      (s, r) => s + (Number(r.performance) || 0) * weightOf(r),
      0,
    );
    return weighted / totalWeight;
  }, [routePerformance]);

  const fleetUtilization = parseMetricValue(metrics?.fleet_utilization?.value);
  const selectedTitle = metrics?.[selectedMetric]?.title ?? "Analytics";
  // F13: the snapshot time goes into the Analytics hub's title-row counts.
  const asOfNode = useMemo(
    () =>
      asOf && !loading && !loadError ? (
        <span className="whitespace-nowrap">
          As of <time dateTime={asOf}>{formatAsOf(asOf)}</time>
        </span>
      ) : null,
    [asOf, loading, loadError],
  );
  const embedded = usePageChrome({ counts: asOfNode });

  const getChartOptions = (type: string) => {
    const baseOptions = {
      backgroundColor: "transparent",
      legend: {
        position: "bottom",
        textStyle: { fontSize: 12, color: COLOR.slate[700] },
        alignment: "center",
      },
      titleTextStyle: { fontSize: 14, bold: true, color: COLOR.slate[900] },
      hAxis: {
        textStyle: { fontSize: 11, color: COLOR.slate[500] },
        gridlines: { color: COLOR.slate[100], count: 5 },
        baselineColor: COLOR.slate[200],
      },
      vAxis: {
        textStyle: { fontSize: 11, color: COLOR.slate[500] },
        gridlines: { color: COLOR.slate[100], count: 5 },
        baselineColor: COLOR.slate[200],
      },
      chartArea: { left: 60, top: 20, width: "85%", height: "75%" },
    };

    switch (type) {
      case "line":
        return {
          ...baseOptions,
          curveType: "function",
          colors: [SEMANTIC.primary],
          pointSize: 6,
          lineWidth: 3,
          pointShape: "circle",
          series: {
            0: {
              areaOpacity: 0.1,
              color: SEMANTIC.primary,
            },
          },
        };
      case "pie":
        return {
          ...baseOptions,
          // Categorical chart order from the tokens (design.md §2.1).
          colors: [...CHART],
          pieSliceText: "percentage",
          pieSliceTextStyle: { fontSize: 11, color: "white", bold: true },
          is3D: false,
          pieHole: 0.3,
          sliceVisibilityThreshold: 0.02,
        };
      case "bar":
        return {
          ...baseOptions,
          colors: [SEMANTIC.primary],
          bar: { groupWidth: "65%" },
          series: {
            0: {
              color: SEMANTIC.primary,
            },
          },
        };
      default:
        return baseOptions;
    }
  };

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="p-4">
        {/* As of: in the hub's title row when hosted, else inline (F13). */}
        {!embedded && asOfNode && (
          <p className="mb-3 text-xs text-text-muted">{asOfNode}</p>
        )}
        {/* Loading State */}
        {loading && (
          <LoadingSpinner message="Loading analytics..." fullHeight={false} />
        )}

        {/* Error state (F13) */}
        {!loading && loadError && (
          <div
            role="alert"
            className="mb-8 rounded-2xl border border-error-light bg-error-light/30 p-6"
          >
            <p className="text-sm font-medium text-gray-800">
              Analytics could not be loaded.
            </p>
            <button
              type="button"
              onClick={() => loadAnalyticsData()}
              className="mt-3 rounded-xl border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-800 hover:border-gray-400"
            >
              Retry
            </button>
          </div>
        )}

        {/* Empty state: no daily snapshot yet (F3) */}
        {!loading && !loadError && !metrics && (
          <div className="mb-8 rounded-2xl border border-gray-200 bg-gray-50 p-6">
            <p className="text-sm text-gray-700">
              No analytics snapshot yet. Metrics appear after the daily snapshot
              scores delivered or failed orders.
            </p>
          </div>
        )}

        {/* Key Metrics */}
        {!loading && !loadError && metricEntries.length > 0 && (
          <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-3">
            {metricEntries.map(([key, metric]) => (
              <button
                type="button"
                key={key}
                aria-pressed={selectedMetric === key}
                className={`text-left p-4 rounded-xl cursor-pointer transition-all border ${
                  selectedMetric === key
                    ? "bg-gray-50 border-primary shadow-sm"
                    : "bg-white border-gray-200 hover:border-gray-300 hover:shadow-sm"
                }`}
                onClick={() => setSelectedMetric(key)}
              >
                <span className="mb-4 block text-sm font-medium text-text-muted">
                  {metric.title}
                </span>
                <div
                  className={
                    metric.value == null
                      ? "text-lg font-medium text-text-muted"
                      : "text-2xl font-semibold tabular-nums text-text"
                  }
                >
                  {metric.value ?? NOT_AVAILABLE}
                </div>
              </button>
            ))}
          </div>
        )}

        {/* Interactive Charts */}
        {!loading && !loadError && metrics && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
            {/* Time Series Chart: real daily snapshots (F3) */}
            <div className="bg-white rounded-2xl p-6 border border-gray-200 hover:border-gray-300 transition-colors">
              <div className="flex items-center gap-3 mb-6">
                <div
                  aria-hidden="true"
                  className="h-4 w-1 rounded-full bg-primary"
                />
                <h3 className="text-sm font-semibold text-text">
                  {selectedTitle}, last 30 days
                </h3>
              </div>
              {series === null ? (
                <LoadingSpinner message="Loading trend..." fullHeight={false} />
              ) : hasTrendPoint ? (
                <GoogleChart
                  chartType="LineChart"
                  data={chartData.timeSeriesData}
                  options={getChartOptions("line")}
                  height="280px"
                />
              ) : (
                <p className="text-sm text-gray-500">Not enough history yet</p>
              )}
            </div>

            {/* Fleet Utilization Gauge (no placeholder value, F2/F3) */}
            <div className="bg-white border border-gray-200 rounded-2xl p-6 hover:border-gray-300 transition-colors">
              <div className="flex items-center gap-3 mb-6">
                <div
                  aria-hidden="true"
                  className="h-4 w-1 rounded-full bg-primary"
                />
                <h3 className="text-sm font-semibold text-text">
                  Fleet Utilization
                </h3>
              </div>
              {fleetUtilization != null ? (
                <GoogleChart
                  chartType="Gauge"
                  data={[
                    ["Label", "Value"],
                    ["Utilization", fleetUtilization],
                  ]}
                  options={{
                    width: "100%",
                    height: 220,
                    redFrom: 0,
                    redTo: 25,
                    yellowFrom: 25,
                    yellowTo: 75,
                    greenFrom: 75,
                    greenTo: 100,
                    minorTicks: 5,
                    majorTicks: ["0", "25", "50", "75", "100"],
                    animation: { duration: 1000, easing: "out" },
                  }}
                />
              ) : (
                <p className="text-sm text-gray-500">{NOT_AVAILABLE}</p>
              )}
            </div>
          </div>
        )}

        {/* Route Performance */}
        {!loading && !loadError && routePerformance.length > 0 && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
            <div className="bg-white rounded-2xl p-6 border border-gray-200 hover:border-gray-300 transition-colors">
              <div className="flex items-center gap-3 mb-6">
                <div
                  aria-hidden="true"
                  className="h-4 w-1 rounded-full bg-primary"
                />
                <h3 className="text-sm font-semibold text-text">
                  Route Performance Comparison
                </h3>
              </div>
              <GoogleChart
                chartType="ColumnChart"
                data={chartData.barChartData}
                options={getChartOptions("bar")}
                height="320px"
              />
            </div>
            <div className="bg-white rounded-2xl p-6 border border-gray-200 hover:border-gray-300 transition-colors">
              <div className="flex items-center gap-3 mb-6">
                <div
                  aria-hidden="true"
                  className="h-4 w-1 rounded-full bg-primary"
                />
                <h3 className="text-sm font-semibold text-text">
                  Route Performance Mix
                </h3>
              </div>
              <GoogleChart
                chartType="PieChart"
                data={chartData.pieChartData}
                options={getChartOptions("pie")}
                height="320px"
              />
            </div>
          </div>
        )}

        {/* Key Insights */}
        {!loading && !loadError && routePerformance.length > 0 && (
          <div className="bg-gray-50 border border-gray-200 rounded-2xl p-6">
            <div className="flex items-center gap-3 mb-6">
              <Activity className="w-5 h-5 text-primary" />
              <h3 className="text-sm font-semibold text-text">Key Insights</h3>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="space-y-4">
                <div className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-200">
                  <div className="w-2 h-2 bg-success rounded-full"></div>
                  <span className="text-sm font-medium text-gray-700">
                    Best route:{" "}
                    <span className="text-primary font-semibold">
                      {sortedRoutes[0]?.name}
                    </span>{" "}
                    ({sortedRoutes[0]?.performance}%)
                  </span>
                </div>
                {sortedRoutes.length >= 2 && (
                  <div className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-200">
                    <div className="w-2 h-2 bg-error rounded-full"></div>
                    <span className="text-sm font-medium text-gray-700">
                      Needs attention:{" "}
                      <span className="text-primary font-semibold">
                        {sortedRoutes[sortedRoutes.length - 1]?.name}
                      </span>{" "}
                      ({sortedRoutes[sortedRoutes.length - 1]?.performance}%)
                    </span>
                  </div>
                )}
              </div>
              <div className="space-y-4">
                <div className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-200">
                  <div className="w-2 h-2 bg-warning rounded-full"></div>
                  <span className="text-sm font-medium text-gray-700">
                    Average route performance:{" "}
                    <span className="text-primary font-semibold">
                      {averageRoutePerformance != null
                        ? `${number(averageRoutePerformance, { decimals: 1 })}%`
                        : NOT_AVAILABLE}
                    </span>
                  </span>
                </div>
                <div className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-200">
                  <div className="w-2 h-2 bg-info rounded-full"></div>
                  <span className="text-sm font-medium text-gray-700">
                    Fleet utilization:{" "}
                    <span className="text-primary font-semibold">
                      {metrics?.fleet_utilization?.value ?? NOT_AVAILABLE}
                    </span>
                  </span>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
