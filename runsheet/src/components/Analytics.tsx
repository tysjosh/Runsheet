import { Activity, BarChart3, Download, TrendingUp } from "lucide-react";
import React, { useEffect, useMemo, useState } from "react";
import { colors } from "@/styles/design-tokens";
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
        className="flex items-center justify-center text-gray-400 text-sm"
      >
        Loading chart...
      </div>
    );
  }

  return <div ref={chartRef} style={{ width, height }} />;
}

export default function Analytics() {
  const [timeRange, setTimeRange] = useState("7d");
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
        apiService.getAnalyticsMetrics(timeRange),
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

  useEffect(() => {
    loadAnalyticsData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeRange]);

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
        textStyle: { fontSize: 12, color: colors.gray[700] },
        alignment: "center",
      },
      titleTextStyle: { fontSize: 14, bold: true, color: colors.gray[900] },
      hAxis: {
        textStyle: { fontSize: 11, color: colors.gray[500] },
        gridlines: { color: colors.gray[100], count: 5 },
        baselineColor: colors.gray[200],
      },
      vAxis: {
        textStyle: { fontSize: 11, color: colors.gray[500] },
        gridlines: { color: colors.gray[100], count: 5 },
        baselineColor: colors.gray[200],
      },
      chartArea: { left: 60, top: 20, width: "85%", height: "75%" },
    };

    switch (type) {
      case "line":
        return {
          ...baseOptions,
          curveType: "function",
          colors: [colors.primary.DEFAULT],
          pointSize: 6,
          lineWidth: 3,
          pointShape: "circle",
          series: {
            0: {
              areaOpacity: 0.1,
              color: colors.primary.DEFAULT,
            },
          },
        };
      case "pie":
        return {
          ...baseOptions,
          colors: [
            colors.primary.DEFAULT,
            colors.gray[500],
            colors.gray[400],
            colors.gray[300],
            colors.gray[200],
            colors.gray[100],
          ],
          pieSliceText: "percentage",
          pieSliceTextStyle: { fontSize: 11, color: "white", bold: true },
          is3D: false,
          pieHole: 0.3,
          sliceVisibilityThreshold: 0.02,
        };
      case "bar":
        return {
          ...baseOptions,
          colors: [colors.primary.DEFAULT],
          bar: { groupWidth: "65%" },
          series: {
            0: {
              color: colors.primary.DEFAULT,
            },
          },
        };
      default:
        return baseOptions;
    }
  };

  return (
    <div className="h-full overflow-y-auto bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-primary rounded-xl flex items-center justify-center">
              <BarChart3 className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-2xl font-semibold text-primary">
                Analytics Dashboard
              </h1>
              <p className="text-gray-500">
                Performance insights and operational metrics
              </p>
            </div>
          </div>
          <div className="flex gap-3">
            <select
              value={timeRange}
              onChange={(e) => setTimeRange(e.target.value)}
              className="px-4 py-2 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white font-medium"
            >
              <option value="24h">Last 24 Hours</option>
              <option value="7d">Last 7 Days</option>
              <option value="30d">Last 30 Days</option>
              <option value="90d">Last 90 Days</option>
            </select>
            <button className="bg-primary hover:bg-primary-hover text-white px-6 py-2 rounded-xl text-sm font-medium transition-colors flex items-center gap-2">
              <Download className="w-4 h-4" />
              Export Report
            </button>
          </div>
        </div>
      </div>

      <div className="p-8">
        {/* Loading State */}
        {loading && (
          <LoadingSpinner message="Loading analytics..." fullHeight={false} />
        )}

        {/* Key Metrics */}
        {!loading && metrics && (
          <div className="grid grid-cols-4 gap-6 mb-8">
            {Object.entries(metrics).map(([key, metric], _index) => {
              return (
                <div
                  key={key}
                  className={`p-6 rounded-2xl cursor-pointer transition-all border ${
                    selectedMetric === key
                      ? "bg-gray-50 border-primary shadow-sm"
                      : "bg-white border-gray-200 hover:border-gray-300 hover:shadow-sm"
                  }`}
                  onClick={() => setSelectedMetric(key)}
                >
                  <div className="flex items-start justify-between mb-4">
                    <h3 className="text-sm font-medium text-gray-600">
                      {metric.title}
                    </h3>
                    <div
                      className={`flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium ${
                        metric.trend === "up"
                          ? "text-success-dark bg-success-light"
                          : "text-error-dark bg-error-light"
                      }`}
                    >
                      <TrendingUp
                        className={`w-3 h-3 ${metric.trend === "down" ? "rotate-180" : ""}`}
                      />
                      <span>{metric.change}</span>
                    </div>
                  </div>
                  <div className="text-3xl font-semibold text-primary mb-1">
                    {metric.value}
                  </div>
                </div>
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
                  <div className="w-2 h-2 bg-primary rounded-full"></div>
                  <h3 className="text-lg font-semibold text-primary">
                    {metrics[selectedMetric as keyof typeof metrics]?.title ??
                      "Analytics"}{" "}
                    Trend
                  </h3>
                  <span className="text-sm text-gray-500 bg-gray-100 px-3 py-1 rounded-lg">
                    {timeRange}
                  </span>
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
                  <div className="w-2 h-2 bg-primary rounded-full"></div>
                  <h3 className="text-lg font-semibold text-primary">
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
              <div className="w-2 h-2 bg-primary rounded-full"></div>
              <h3 className="text-lg font-semibold text-primary">
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
                <div className="w-2 h-2 bg-primary rounded-full"></div>
                <h3 className="text-lg font-semibold text-primary">
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
                <div className="w-2 h-2 bg-primary rounded-full"></div>
                <h3 className="text-lg font-semibold text-primary">
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
              <h3 className="text-lg font-semibold text-primary">
                Key Insights
              </h3>
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
                      {(
                        routePerformance.reduce(
                          (sum, route) =>
                            sum + (Number(route.performance) || 0),
                          0,
                        ) / routePerformance.length
                      ).toFixed(1)}
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
