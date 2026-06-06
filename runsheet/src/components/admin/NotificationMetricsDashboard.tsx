"use client";

import {
  Activity,
  AlertCircle,
  Bell,
  Clock,
  Send,
  TrendingUp,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  type CommunicationMetrics,
  getCommunicationMetrics,
  type MetricDataPoint,
  type MetricsFilters,
} from "../../services/adminApi";
import { Badge, Button, PageHeader } from "../ui";

// ─── Helper Functions ────────────────────────────────────────────────────────

function formatLatency(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function formatPercentage(rate: number): string {
  return `${(rate * 100).toFixed(2)}%`;
}

function getLatestValue(dataPoints: MetricDataPoint[]): number | null {
  if (!dataPoints || !Array.isArray(dataPoints) || dataPoints.length === 0)
    return null;
  return dataPoints[dataPoints.length - 1]?.value ?? null;
}

function calculateAverage(dataPoints: MetricDataPoint[]): number | null {
  if (!dataPoints || !Array.isArray(dataPoints) || dataPoints.length === 0)
    return null;
  const sum = dataPoints.reduce((acc, dp) => acc + (dp.value ?? 0), 0);
  return sum / dataPoints.length;
}

function getStatusBadge(
  value: number | null,
  thresholds: { warning: number; critical: number },
  isRate = false,
) {
  if (value === null) return <Badge variant="default">No Data</Badge>;

  if (isRate) {
    // For failure rates, higher is worse
    if (value >= thresholds.critical)
      return <Badge variant="error">Critical</Badge>;
    if (value >= thresholds.warning)
      return <Badge variant="warning">Warning</Badge>;
    return <Badge variant="success">Healthy</Badge>;
  } else {
    // For latencies, higher is worse
    if (value >= thresholds.critical)
      return <Badge variant="error">Critical</Badge>;
    if (value >= thresholds.warning)
      return <Badge variant="warning">Warning</Badge>;
    return <Badge variant="success">Healthy</Badge>;
  }
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function NotificationMetricsDashboard() {
  const [metrics, setMetrics] = useState<CommunicationMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [interval, setInterval] = useState<string>("1d");
  const [dateRange, setDateRange] = useState<{ start: string; end: string }>({
    start: new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)
      .toISOString()
      .split("T")[0],
    end: new Date().toISOString().split("T")[0],
  });

  const fetchMetrics = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: MetricsFilters = {
        start_date: dateRange.start,
        end_date: dateRange.end,
        interval,
      };
      const data = await getCommunicationMetrics(filters);
      setMetrics(data);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load communication metrics",
      );
    } finally {
      setLoading(false);
    }
  }, [dateRange, interval]);

  useEffect(() => {
    fetchMetrics();
  }, [fetchMetrics]);

  // Calculate summary stats
  const ackLatencyLatest = metrics
    ? getLatestValue(
        Array.isArray(metrics.ack_latency) ? metrics.ack_latency : [],
      )
    : null;
  const ackLatencyAvg = metrics
    ? calculateAverage(
        Array.isArray(metrics.ack_latency) ? metrics.ack_latency : [],
      )
    : null;

  const sendLatencyLatest = metrics
    ? getLatestValue(
        Array.isArray(metrics.notification_send_latency)
          ? metrics.notification_send_latency
          : [],
      )
    : null;
  const sendLatencyAvg = metrics
    ? calculateAverage(
        Array.isArray(metrics.notification_send_latency)
          ? metrics.notification_send_latency
          : [],
      )
    : null;

  const responseLatencyLatest = metrics
    ? getLatestValue(
        Array.isArray(metrics.driver_response_latency)
          ? metrics.driver_response_latency
          : [],
      )
    : null;
  const responseLatencyAvg = metrics
    ? calculateAverage(
        Array.isArray(metrics.driver_response_latency)
          ? metrics.driver_response_latency
          : [],
      )
    : null;

  const failureRateLatest = metrics
    ? getLatestValue(
        Array.isArray(metrics.failed_notification_rate)
          ? metrics.failed_notification_rate
          : [],
      )
    : null;
  const failureRateAvg = metrics
    ? calculateAverage(
        Array.isArray(metrics.failed_notification_rate)
          ? metrics.failed_notification_rate
          : [],
      )
    : null;

  return (
    <div className="p-6">
      <PageHeader
        title="Communication Metrics"
        subtitle="Monitor notification delivery performance and SLA compliance"
        icon={<Activity className="w-5 h-5" />}
      />

      {/* Controls */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 mb-6">
        <div className="flex flex-wrap gap-4 items-end">
          <div className="flex-1 min-w-[200px]">
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Start Date
            </label>
            <input
              type="date"
              value={dateRange.start}
              onChange={(e) =>
                setDateRange((prev) => ({ ...prev, start: e.target.value }))
              }
              className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
            />
          </div>

          <div className="flex-1 min-w-[200px]">
            <label className="block text-sm font-medium text-gray-700 mb-2">
              End Date
            </label>
            <input
              type="date"
              value={dateRange.end}
              onChange={(e) =>
                setDateRange((prev) => ({ ...prev, end: e.target.value }))
              }
              className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
            />
          </div>

          <div className="flex-1 min-w-[200px]">
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Interval
            </label>
            <select
              value={interval}
              onChange={(e) => setInterval(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
            >
              <option value="1h">Hourly</option>
              <option value="1d">Daily</option>
              <option value="1w">Weekly</option>
            </select>
          </div>

          <Button onClick={fetchMetrics} disabled={loading}>
            {loading ? "Loading..." : "Refresh"}
          </Button>
        </div>
      </div>

      {/* Error State */}
      {error && (
        <div className="bg-error-light border border-error text-error-dark p-4 rounded-lg mb-6 flex items-center gap-2">
          <AlertCircle className="w-5 h-5" />
          <span>{error}</span>
        </div>
      )}

      {/* Loading State */}
      {loading && (
        <div className="flex justify-center py-12">
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      )}

      {/* Metrics Display */}
      {!loading && metrics && (
        <>
          {/* Summary Cards */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            {/* Ack Latency */}
            <div className="bg-white rounded-xl border border-gray-200 p-4">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <Clock className="w-4 h-4 text-blue-500" />
                  <span className="text-sm font-medium text-gray-700">
                    Ack Latency
                  </span>
                </div>
                {getStatusBadge(ackLatencyLatest, {
                  warning: 5000,
                  critical: 10000,
                })}
              </div>
              <div className="text-2xl font-bold text-gray-900 mb-1">
                {ackLatencyLatest !== null
                  ? formatLatency(ackLatencyLatest)
                  : "—"}
              </div>
              <div className="text-xs text-gray-500">
                Avg:{" "}
                {ackLatencyAvg !== null ? formatLatency(ackLatencyAvg) : "—"}
              </div>
            </div>

            {/* Send Latency */}
            <div className="bg-white rounded-xl border border-gray-200 p-4">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <Send className="w-4 h-4 text-green-500" />
                  <span className="text-sm font-medium text-gray-700">
                    Send Latency
                  </span>
                </div>
                {getStatusBadge(sendLatencyLatest, {
                  warning: 3000,
                  critical: 5000,
                })}
              </div>
              <div className="text-2xl font-bold text-gray-900 mb-1">
                {sendLatencyLatest !== null
                  ? formatLatency(sendLatencyLatest)
                  : "—"}
              </div>
              <div className="text-xs text-gray-500">
                Avg:{" "}
                {sendLatencyAvg !== null ? formatLatency(sendLatencyAvg) : "—"}
              </div>
            </div>

            {/* Driver Response Latency */}
            <div className="bg-white rounded-xl border border-gray-200 p-4">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <TrendingUp className="w-4 h-4 text-purple-500" />
                  <span className="text-sm font-medium text-gray-700">
                    Response Latency
                  </span>
                </div>
                {getStatusBadge(responseLatencyLatest, {
                  warning: 300000,
                  critical: 600000,
                })}
              </div>
              <div className="text-2xl font-bold text-gray-900 mb-1">
                {responseLatencyLatest !== null
                  ? formatLatency(responseLatencyLatest)
                  : "—"}
              </div>
              <div className="text-xs text-gray-500">
                Avg:{" "}
                {responseLatencyAvg !== null
                  ? formatLatency(responseLatencyAvg)
                  : "—"}
              </div>
            </div>

            {/* Failure Rate */}
            <div className="bg-white rounded-xl border border-gray-200 p-4">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <Bell className="w-4 h-4 text-red-500" />
                  <span className="text-sm font-medium text-gray-700">
                    Failure Rate
                  </span>
                </div>
                {getStatusBadge(
                  failureRateLatest,
                  { warning: 0.05, critical: 0.1 },
                  true,
                )}
              </div>
              <div className="text-2xl font-bold text-gray-900 mb-1">
                {failureRateLatest !== null
                  ? formatPercentage(failureRateLatest)
                  : "—"}
              </div>
              <div className="text-xs text-gray-500">
                Avg:{" "}
                {failureRateAvg !== null
                  ? formatPercentage(failureRateAvg)
                  : "—"}
              </div>
            </div>
          </div>

          {/* Detailed Charts */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Ack Latency Chart */}
            <MetricChart
              title="Acknowledgment Latency"
              data={
                Array.isArray(metrics.ack_latency) ? metrics.ack_latency : []
              }
              formatValue={formatLatency}
              color="blue"
            />

            {/* Send Latency Chart */}
            <MetricChart
              title="Notification Send Latency"
              data={
                Array.isArray(metrics.notification_send_latency)
                  ? metrics.notification_send_latency
                  : []
              }
              formatValue={formatLatency}
              color="green"
            />

            {/* Response Latency Chart */}
            <MetricChart
              title="Driver Response Latency"
              data={
                Array.isArray(metrics.driver_response_latency)
                  ? metrics.driver_response_latency
                  : []
              }
              formatValue={formatLatency}
              color="purple"
            />

            {/* Failure Rate Chart */}
            <MetricChart
              title="Failed Notification Rate"
              data={
                Array.isArray(metrics.failed_notification_rate)
                  ? metrics.failed_notification_rate
                  : []
              }
              formatValue={formatPercentage}
              color="red"
            />
          </div>
        </>
      )}
    </div>
  );
}

// ─── Metric Chart Component ──────────────────────────────────────────────────

interface MetricChartProps {
  title: string;
  data: MetricDataPoint[];
  formatValue: (value: number) => string;
  color: "blue" | "green" | "purple" | "red";
}

function MetricChart({ title, data, formatValue, color }: MetricChartProps) {
  const colorClasses = {
    blue: "bg-blue-500",
    green: "bg-green-500",
    purple: "bg-purple-500",
    red: "bg-red-500",
  };

  const maxValue = Math.max(...data.map((d) => d.value), 1);

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">{title}</h3>

      {data.length === 0 ? (
        <div className="text-center py-8 text-gray-500">No data available</div>
      ) : (
        <div className="space-y-2">
          {data.map((point, index) => (
            <div key={index} className="flex items-center gap-3">
              <div className="text-xs text-gray-500 w-32 flex-shrink-0">
                {new Date(point.timestamp).toLocaleString(undefined, {
                  month: "short",
                  day: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </div>
              <div className="flex-1 bg-gray-100 rounded-full h-6 relative overflow-hidden">
                <div
                  className={`${colorClasses[color]} h-full rounded-full transition-all duration-300`}
                  style={{ width: `${(point.value / maxValue) * 100}%` }}
                />
              </div>
              <div className="text-sm font-medium text-gray-900 w-20 text-right">
                {formatValue(point.value)}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
