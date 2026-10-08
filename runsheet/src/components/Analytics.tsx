import { Activity, TrendingUp } from "lucide-react";
import React, { useEffect, useMemo, useState } from "react";
import { CHART, COLOR, SEMANTIC } from "@/styles/tokens";
import { number } from "../lib/format";
import { type AnalyticsMetrics, apiService } from "../services/api";
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

interface RoutePerformance {
  name: string;
  performance: number;
}

function getMetricLabel(metric: string) {
  const labels = {
    delivery_performance: "Performance (%)",
    average_delay: "Delay (minutes)",
    fleet_utilization: "Utilization (%)",
    customer_satisfaction: "Rating (1-5)",
  };
  return labels[metric as keyof typeof labels] || "Value";
}

function parseMetricValue(value?: string): number {
  if (!value) return 0;
  const numeric = Number.parseFloat(value.replace(/[^0-9.-]/g, ""));
  return Number.isFinite(numeric) ? numeric : 0;
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
  const [selectedMetric, setSelectedMetric] = useState("delivery_performance");
  const [metrics, setMetrics] = useState<AnalyticsMetrics | null>(null);
  const [routePerformance, setRoutePerformance] = useState<RoutePerformance[]>(
    [],
  );
  const [loading, setLoading] = useState(true);

  const loadAnalyticsData = async () => {
    try {
      setLoading(true);
      const [metricsResponse, routesResponse] = await Promise.all([
        apiService.getAnalyticsMetrics(),
        apiService.getAnalyticsRoutePerformance(),
      ]);

      setMetrics(metricsResponse.data);
      setRoutePerformance(routesResponse.data);
    } catch (error) {
      console.error("Failed to load analytics data:", error);
    } finally {
      setLoading(false);
    }
  };

  // Loads once on mount. There is no time-range selector: the backend
  // ignores ranges (B6), so offering one would show the same numbers under
  // a different label (OI-50).
  useEffect(() => {
    loadAnalyticsData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const chartData = useMemo(() => {
    const metric = metrics?.[selectedMetric as keyof AnalyticsMetrics];
    const currentValue = parseMetricValue(metric?.value);
    const changeValue = parseMetricValue(metric?.change);
    const previousValue =
      metric?.trend === "down"
        ? currentValue + changeValue
        : currentValue - changeValue;

    return {
      timeSeriesData: [
        [
          { type: "string", label: "Period" },
          { type: "number", label: getMetricLabel(selectedMetric) },
        ],
        ["Previous", Math.max(0, previousValue)],
        ["Current", Math.max(0, currentValue)],
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
    };
  }, [metrics, routePerformance, selectedMetric]);

  const sortedRoutes = useMemo(
    () => [...routePerformance].sort((a, b) => b.performance - a.performance),
    [routePerformance],
  );

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
        {/* Loading State */}
        {loading && (
          <LoadingSpinner message="Loading analytics..." fullHeight={false} />
        )}

        {/* Key Metrics */}
        {!loading && metrics && (
          <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            {Object.entries(metrics).map(([key, metric], _index) => {
              return (
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
                  <div className="flex items-start justify-between mb-4">
                    <span className="text-sm font-medium text-text-muted">
                      {metric.title}
                    </span>
                    <div
                      className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-semibold ${
                        metric.trend === "up"
                          ? "border-brand-300 bg-brand-100 text-brand-800"
                          : "border-red-300 bg-red-100 text-red-800"
                      }`}
                    >
                      <TrendingUp
                        className={`w-3 h-3 ${metric.trend === "down" ? "rotate-180" : ""}`}
                      />
                      <span>{metric.change}</span>
                    </div>
                  </div>
                  <div className="text-2xl font-semibold tabular-nums text-text">
                    {metric.value}
                  </div>
                </button>
              );
            })}
          </div>
        )}

        {/* Interactive Charts */}
        {!loading &&
          metrics &&
          Object.keys(metrics).length > 0 &&
          chartData && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
              {/* Time Series Chart */}
              <div className="bg-white rounded-2xl p-6 border border-gray-200 hover:border-gray-300 transition-colors">
                <div className="flex items-center gap-3 mb-6">
                  <div
                    aria-hidden="true"
                    className="h-4 w-1 rounded-full bg-primary"
                  />
                  <h3 className="text-sm font-semibold text-text">
                    {metrics[selectedMetric as keyof typeof metrics]?.title ??
                      "Analytics"}{" "}
                    Trend
                  </h3>
                </div>
                <GoogleChart
                  chartType="LineChart"
                  data={chartData.timeSeriesData}
                  options={getChartOptions("line")}
                  height="280px"
                />
              </div>

              {/* Route Mix Pie Chart */}
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
                  height="280px"
                />
              </div>
            </div>
          )}

        {/* Route Performance Bar Chart */}
        {!loading && chartData && (
          <div className="bg-white rounded-2xl p-6 mb-8 border border-gray-200 hover:border-gray-300 transition-colors">
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
        )}

        {/* Additional Analytics Charts */}
        {!loading && chartData && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
            {/* Fleet Utilization Gauge */}
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
              <GoogleChart
                chartType="Gauge"
                data={[
                  ["Label", "Value"],
                  [
                    "Utilization",
                    metrics?.fleet_utilization
                      ? parseFloat(
                          metrics.fleet_utilization.value.replace("%", ""),
                        )
                      : 92,
                  ],
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
            </div>

            {/* Customer Satisfaction Gauge */}
            <div className="bg-white border border-gray-200 rounded-2xl p-6 hover:border-gray-300 transition-colors">
              <div className="flex items-center gap-3 mb-6">
                <div
                  aria-hidden="true"
                  className="h-4 w-1 rounded-full bg-primary"
                />
                <h3 className="text-sm font-semibold text-text">
                  Customer Satisfaction
                </h3>
              </div>
              <GoogleChart
                chartType="Gauge"
                data={[
                  ["Label", "Value"],
                  [
                    "Rating",
                    metrics?.customer_satisfaction
                      ? parseFloat(
                          metrics.customer_satisfaction.value.split("/")[0],
                        )
                      : 4.2,
                  ],
                ]}
                options={{
                  width: "100%",
                  height: 220,
                  max: 5,
                  redFrom: 0,
                  redTo: 2,
                  yellowFrom: 2,
                  yellowTo: 3.5,
                  greenFrom: 3.5,
                  greenTo: 5,
                  minorTicks: 5,
                  majorTicks: ["0", "1", "2", "3", "4", "5"],
                  animation: { duration: 1000, easing: "out" },
                }}
              />
            </div>
          </div>
        )}

        {/* Key Insights */}
        {!loading && routePerformance.length > 0 && (
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
              </div>
              <div className="space-y-4">
                <div className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-200">
                  <div className="w-2 h-2 bg-warning rounded-full"></div>
                  <span className="text-sm font-medium text-gray-700">
                    Average route performance:{" "}
                    <span className="text-primary font-semibold">
                      {number(
                        routePerformance.reduce(
                          (sum, route) =>
                            sum + (Number(route.performance) || 0),
                          0,
                        ) / routePerformance.length,
                        { decimals: 1 },
                      )}
                      %
                    </span>
                  </span>
                </div>
                <div className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-200">
                  <div className="w-2 h-2 bg-info rounded-full"></div>
                  <span className="text-sm font-medium text-gray-700">
                    Fleet utilization:{" "}
                    <span className="text-primary font-semibold">
                      {metrics?.fleet_utilization?.value || "N/A"}
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
