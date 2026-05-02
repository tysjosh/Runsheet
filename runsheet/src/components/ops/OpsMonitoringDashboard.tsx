"use client";

/**
 * Ops Monitoring Dashboard — Ingestion, Indexing & Poison Queue health,
 * Shipment Metrics, and SLA Compliance.
 *
 * Three-card grid layout displaying pipeline health metrics with color-coded
 * values (green/yellow/red) and auto-refresh every 30 seconds.
 * Below the pipeline cards: shipment volume time-series table and SLA summary cards.
 *
 * Validates:
 * - Requirement 6.1: Display ingestion metrics via getIngestionMonitoring
 * - Requirement 6.2: Display indexing metrics via getIndexingMonitoring
 * - Requirement 6.3: Display poison queue metrics via getPoisonQueueMonitoring
 * - Requirement 6.4: Color-code metric values green/yellow/red based on thresholds
 * - Requirement 6.5: Visual alert indicator next to metrics exceeding critical thresholds
 * - Requirement 6.6: Auto-refresh every 30 seconds with polling interval
 * - Requirement 8.1: Shipment metrics time-series display
 * - Requirement 8.2: Configurable bucket and date range for shipment metrics
 * - Requirement 8.3: SLA compliance summary cards
 * - Requirement 8.4: Fetch SLA metrics from getSlaMetrics()
 * - Requirement 8.5: Re-fetch shipment metrics on filter changes
 * - Requirement 8.6: Per-section error handling
 * - Requirement 8.7: Retain existing pipeline health monitoring
 */

import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle,
  Database,
  Loader2,
  RefreshCw,
  Shield,
  Skull,
  TrendingUp,
  XCircle,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type {
  IngestionMetrics,
  IndexingMetrics,
  MetricsBucket,
  MetricsFilters,
  MetricsResponse,
  PoisonQueueMetrics,
} from "../../services/opsApi";
import {
  getIngestionMonitoring,
  getIndexingMonitoring,
  getPoisonQueueMonitoring,
  getShipmentMetrics,
  getSlaMetrics,
} from "../../services/opsApi";

// ─── Metric Status Types & Helper ────────────────────────────────────────────

export type MetricStatus = "healthy" | "degraded" | "critical";

export interface ThresholdConfig {
  /** Direction of comparison: "above" means higher values are worse, "below" means lower values are worse */
  direction: "above" | "below";
  /** Warning threshold — value at which status becomes "degraded" */
  warning: number;
  /** Critical threshold — value at which status becomes "critical" */
  critical: number;
}

/**
 * Determine the health status of a metric value based on threshold configuration.
 *
 * For "above" direction (e.g. error counts, queue depth):
 *   value > critical → "critical", value > warning → "degraded", else → "healthy"
 *
 * For "below" direction (e.g. success rates):
 *   value < critical → "critical", value < warning → "degraded", else → "healthy"
 */
export function getMetricStatus(
  value: number,
  config: ThresholdConfig,
): MetricStatus {
  if (config.direction === "above") {
    if (value > config.critical) return "critical";
    if (value > config.warning) return "degraded";
    return "healthy";
  }
  // direction === "below"
  if (value < config.critical) return "critical";
  if (value < config.warning) return "degraded";
  return "healthy";
}

// ─── Threshold Configurations ────────────────────────────────────────────────

const INGESTION_THRESHOLDS: Record<string, ThresholdConfig> = {
  events_failed: { direction: "above", warning: 50, critical: 100 },
};

const INDEXING_THRESHOLDS: Record<string, ThresholdConfig> = {
  bulk_success_rate: { direction: "below", warning: 0.99, critical: 0.95 },
};

const POISON_QUEUE_THRESHOLDS: Record<string, ThresholdConfig> = {
  queue_depth: { direction: "above", warning: 50, critical: 100 },
};

// ─── Status Styling ──────────────────────────────────────────────────────────

const STATUS_STYLES: Record<MetricStatus, { text: string; bg: string }> = {
  healthy: { text: "text-green-600", bg: "bg-green-50" },
  degraded: { text: "text-yellow-600", bg: "bg-yellow-50" },
  critical: { text: "text-red-600", bg: "bg-red-50" },
};

// ─── Polling Interval ────────────────────────────────────────────────────────

const REFRESH_INTERVAL_MS = 30_000;

// ─── Metric Display Item ─────────────────────────────────────────────────────

interface MetricItemProps {
  label: string;
  value: number | string;
  status: MetricStatus;
}

function MetricItem({ label, value, status }: MetricItemProps) {
  const style = STATUS_STYLES[status];
  return (
    <div className="flex items-center justify-between py-2">
      <span className="text-sm text-gray-600">{label}</span>
      <div className="flex items-center gap-1.5">
        {status === "critical" && (
          <AlertTriangle className="w-3.5 h-3.5 text-red-500" aria-label="Critical alert" />
        )}
        <span
          className={`text-sm font-semibold px-2 py-0.5 rounded ${style.text} ${style.bg}`}
        >
          {typeof value === "number" ? value.toLocaleString() : value}
        </span>
      </div>
    </div>
  );
}

// ─── Metric Card ─────────────────────────────────────────────────────────────

interface MetricCardProps {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  loading: boolean;
}

function MetricCard({ title, icon, children, loading }: MetricCardProps) {
  return (
    <div className="bg-white border border-gray-200 rounded-lg">
      <div className="flex items-center gap-2 px-5 py-4 border-b border-gray-100">
        <div className="w-8 h-8 bg-[#232323] rounded-lg flex items-center justify-center">
          {icon}
        </div>
        <h3 className="text-sm font-semibold text-[#232323]">{title}</h3>
      </div>
      <div className="px-5 py-4">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 text-gray-400 animate-spin" />
          </div>
        ) : (
          <div className="divide-y divide-gray-50">{children}</div>
        )}
      </div>
    </div>
  );
}

// ─── Shipment Metrics Section ────────────────────────────────────────────────

interface ShipmentMetricsSectionProps {
  data: MetricsResponse | null;
  loading: boolean;
  error: string;
  filters: MetricsFilters;
  onFiltersChange: (filters: MetricsFilters) => void;
}

function ShipmentMetricsSection({
  data,
  loading,
  error,
  filters,
  onFiltersChange,
}: ShipmentMetricsSectionProps) {
  const handleBucketChange = (bucket: MetricsBucket) => {
    onFiltersChange({ ...filters, bucket });
  };

  const handleStartDateChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    onFiltersChange({ ...filters, start_date: e.target.value || undefined });
  };

  const handleEndDateChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    onFiltersChange({ ...filters, end_date: e.target.value || undefined });
  };

  // Collect all unique status keys from values data (excluding 'total')
  const statusKeys: string[] = [];
  if (data?.data) {
    const keySet = new Set<string>();
    for (const entry of data.data) {
      if (entry.values) {
        for (const key of Object.keys(entry.values)) {
          if (key !== "total") keySet.add(key);
        }
      }
    }
    statusKeys.push(...Array.from(keySet).sort());
  }

  return (
    <div className="bg-white border border-gray-200 rounded-lg">
      <div className="flex items-center gap-2 px-5 py-4 border-b border-gray-100">
        <div className="w-8 h-8 bg-[#232323] rounded-lg flex items-center justify-center">
          <BarChart3 className="w-4 h-4 text-white" />
        </div>
        <h3 className="text-sm font-semibold text-[#232323]">
          Shipment Metrics
        </h3>
      </div>

      {/* Filter Controls */}
      <div className="px-5 py-3 border-b border-gray-100 flex flex-wrap items-center gap-4">
        {/* Bucket Toggle */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">Bucket:</span>
          <div className="flex rounded-lg border border-gray-200 overflow-hidden">
            <button
              onClick={() => handleBucketChange("hourly")}
              className={`px-3 py-1 text-xs font-medium transition-colors ${
                (filters.bucket ?? "daily") === "hourly"
                  ? "bg-[#232323] text-white"
                  : "bg-white text-gray-600 hover:bg-gray-50"
              }`}
            >
              Hourly
            </button>
            <button
              onClick={() => handleBucketChange("daily")}
              className={`px-3 py-1 text-xs font-medium transition-colors ${
                (filters.bucket ?? "daily") === "daily"
                  ? "bg-[#232323] text-white"
                  : "bg-white text-gray-600 hover:bg-gray-50"
              }`}
            >
              Daily
            </button>
          </div>
        </div>

        {/* Date Range */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">From:</span>
          <input
            type="date"
            value={filters.start_date ?? ""}
            onChange={handleStartDateChange}
            className="text-xs border border-gray-200 rounded-lg px-2 py-1 text-gray-700 focus:outline-none focus:ring-1 focus:ring-[#232323]"
          />
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">To:</span>
          <input
            type="date"
            value={filters.end_date ?? ""}
            onChange={handleEndDateChange}
            className="text-xs border border-gray-200 rounded-lg px-2 py-1 text-gray-700 focus:outline-none focus:ring-1 focus:ring-[#232323]"
          />
        </div>
      </div>

      {/* Content */}
      <div className="px-5 py-4">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 text-gray-400 animate-spin" />
          </div>
        ) : error ? (
          <div className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
            {error}
          </div>
        ) : data && data.data.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100">
                  <th className="text-left py-2 px-3 text-xs font-medium text-gray-500">
                    Timestamp
                  </th>
                  <th className="text-right py-2 px-3 text-xs font-medium text-gray-500">
                    Total
                  </th>
                  {statusKeys.map((key) => (
                    <th
                      key={key}
                      className="text-right py-2 px-3 text-xs font-medium text-gray-500 capitalize"
                    >
                      {key.replace(/_/g, " ")}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {data.data.map((entry, idx) => (
                  <tr key={idx} className="hover:bg-gray-50">
                    <td className="py-2 px-3 text-xs text-gray-700">
                      {new Date(entry.timestamp).toLocaleString()}
                    </td>
                    <td className="py-2 px-3 text-xs text-right font-medium text-gray-900">
                      {(entry.values?.total ?? 0).toLocaleString()}
                    </td>
                    {statusKeys.map((key) => (
                      <td
                        key={key}
                        className="py-2 px-3 text-xs text-right text-gray-600"
                      >
                        {(entry.values?.[key] ?? 0).toLocaleString()}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center py-8 text-gray-400">
            <TrendingUp className="w-8 h-8 mb-2" />
            <p className="text-sm">No shipment metrics available</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── SLA Metrics Section ─────────────────────────────────────────────────────

interface SlaMetricsSectionProps {
  data: MetricsResponse | null;
  loading: boolean;
  error: string;
  filters: MetricsFilters;
  onFiltersChange: (filters: MetricsFilters) => void;
}

function SlaMetricsSection({ data, loading, error, filters, onFiltersChange }: SlaMetricsSectionProps) {
  const handleBucketChange = (bucket: MetricsBucket) => {
    onFiltersChange({ ...filters, bucket });
  };

  const handleStartDateChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    onFiltersChange({ ...filters, start_date: e.target.value || undefined });
  };

  const handleEndDateChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    onFiltersChange({ ...filters, end_date: e.target.value || undefined });
  };

  // Compute aggregate totals from all time buckets
  let totalShipments = 0;
  let totalBreached = 0;
  let totalCompliant = 0;

  if (data?.data) {
    for (const entry of data.data) {
      totalShipments += entry.values?.total ?? 0;
      totalBreached += entry.values?.breached ?? 0;
      totalCompliant += entry.values?.compliant ?? 0;
    }
  }

  const compliancePct =
    totalShipments > 0
      ? ((totalCompliant / totalShipments) * 100).toFixed(1)
      : "100.0";
  const complianceNum = totalShipments > 0 ? totalCompliant / totalShipments : 1;
  const isHealthy = complianceNum >= 0.95;
  const isDegraded = complianceNum >= 0.85 && complianceNum < 0.95;

  return (
    <div className="bg-white border border-gray-200 rounded-lg">
      <div className="flex items-center gap-2 px-5 py-4 border-b border-gray-100">
        <div className="w-8 h-8 bg-[#232323] rounded-lg flex items-center justify-center">
          <Shield className="w-4 h-4 text-white" />
        </div>
        <h3 className="text-sm font-semibold text-[#232323]">
          SLA Compliance
        </h3>
      </div>

      {/* Filter Controls */}
      <div className="px-5 py-3 border-b border-gray-100 flex flex-wrap items-center gap-4">
        {/* Bucket Toggle */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">Bucket:</span>
          <div className="flex rounded-lg border border-gray-200 overflow-hidden">
            <button
              onClick={() => handleBucketChange("hourly")}
              className={`px-3 py-1 text-xs font-medium transition-colors ${
                (filters.bucket ?? "daily") === "hourly"
                  ? "bg-[#232323] text-white"
                  : "bg-white text-gray-600 hover:bg-gray-50"
              }`}
            >
              Hourly
            </button>
            <button
              onClick={() => handleBucketChange("daily")}
              className={`px-3 py-1 text-xs font-medium transition-colors ${
                (filters.bucket ?? "daily") === "daily"
                  ? "bg-[#232323] text-white"
                  : "bg-white text-gray-600 hover:bg-gray-50"
              }`}
            >
              Daily
            </button>
          </div>
        </div>

        {/* Date Range */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">From:</span>
          <input
            type="date"
            value={filters.start_date ?? ""}
            onChange={handleStartDateChange}
            className="text-xs border border-gray-200 rounded-lg px-2 py-1 text-gray-700 focus:outline-none focus:ring-1 focus:ring-[#232323]"
          />
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">To:</span>
          <input
            type="date"
            value={filters.end_date ?? ""}
            onChange={handleEndDateChange}
            className="text-xs border border-gray-200 rounded-lg px-2 py-1 text-gray-700 focus:outline-none focus:ring-1 focus:ring-[#232323]"
          />
        </div>
      </div>

      <div className="px-5 py-4">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 text-gray-400 animate-spin" />
          </div>
        ) : error ? (
          <div className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
            {error}
          </div>
        ) : data && data.data.length > 0 ? (
          <div className="space-y-6">
            {/* Aggregate Summary Cards */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="border border-gray-100 rounded-lg p-4 text-center">
                <p className="text-xs text-gray-500 mb-1">Total Shipments</p>
                <p className="text-xl font-semibold text-gray-900">
                  {totalShipments.toLocaleString()}
                </p>
              </div>
              <div className="border border-gray-100 rounded-lg p-4 text-center">
                <p className="text-xs text-gray-500 mb-1 flex items-center justify-center gap-1">
                  <CheckCircle className="w-3 h-3 text-green-500" />
                  Compliant
                </p>
                <p className="text-xl font-semibold text-green-600">
                  {totalCompliant.toLocaleString()}
                </p>
              </div>
              <div className="border border-gray-100 rounded-lg p-4 text-center">
                <p className="text-xs text-gray-500 mb-1 flex items-center justify-center gap-1">
                  <XCircle className="w-3 h-3 text-red-500" />
                  Breached
                </p>
                <p className="text-xl font-semibold text-red-600">
                  {totalBreached.toLocaleString()}
                </p>
              </div>
              <div className="border border-gray-100 rounded-lg p-4 text-center">
                <p className="text-xs text-gray-500 mb-1">Compliance Rate</p>
                <p
                  className={`text-xl font-bold ${
                    isHealthy
                      ? "text-green-600"
                      : isDegraded
                        ? "text-yellow-600"
                        : "text-red-600"
                  }`}
                >
                  {compliancePct}%
                </p>
              </div>
            </div>

            {/* Time-bucketed breakdown table */}
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-100">
                    <th className="text-left py-2 px-3 text-xs font-medium text-gray-500">
                      Timestamp
                    </th>
                    <th className="text-right py-2 px-3 text-xs font-medium text-gray-500">
                      Total
                    </th>
                    <th className="text-right py-2 px-3 text-xs font-medium text-gray-500">
                      Compliant
                    </th>
                    <th className="text-right py-2 px-3 text-xs font-medium text-gray-500">
                      Breached
                    </th>
                    <th className="text-right py-2 px-3 text-xs font-medium text-gray-500">
                      Compliance %
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {data.data.map((entry, idx) => {
                    const pct = entry.values?.compliance_pct ?? 0;
                    const pctHealthy = pct >= 95;
                    const pctDegraded = pct >= 85 && pct < 95;
                    return (
                      <tr key={idx} className="hover:bg-gray-50">
                        <td className="py-2 px-3 text-xs text-gray-700">
                          {new Date(entry.timestamp).toLocaleString()}
                        </td>
                        <td className="py-2 px-3 text-xs text-right font-medium text-gray-900">
                          {(entry.values?.total ?? 0).toLocaleString()}
                        </td>
                        <td className="py-2 px-3 text-xs text-right text-green-600">
                          {(entry.values?.compliant ?? 0).toLocaleString()}
                        </td>
                        <td className="py-2 px-3 text-xs text-right text-red-600">
                          {(entry.values?.breached ?? 0).toLocaleString()}
                        </td>
                        <td className="py-2 px-3 text-xs text-right">
                          <span
                            className={`px-2 py-0.5 rounded text-xs font-medium ${
                              pctHealthy
                                ? "text-green-600 bg-green-50"
                                : pctDegraded
                                  ? "text-yellow-600 bg-yellow-50"
                                  : "text-red-600 bg-red-50"
                            }`}
                          >
                            {pct.toFixed(1)}%
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center py-8 text-gray-400">
            <Shield className="w-8 h-8 mb-2" />
            <p className="text-sm">No SLA metrics available</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Main Dashboard Component ────────────────────────────────────────────────

export default function OpsMonitoringDashboard() {
  const [ingestion, setIngestion] = useState<IngestionMetrics | null>(null);
  const [indexing, setIndexing] = useState<IndexingMetrics | null>(null);
  const [poisonQueue, setPoisonQueue] = useState<PoisonQueueMetrics | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [secondsAgo, setSecondsAgo] = useState(0);
  const lastUpdatedRef = useRef<Date | null>(null);

  // ─── Shipment Metrics State ────────────────────────────────────────────
  const [shipmentMetrics, setShipmentMetrics] =
    useState<MetricsResponse | null>(null);
  const [shipmentLoading, setShipmentLoading] = useState(true);
  const [shipmentError, setShipmentError] = useState("");
  const [shipmentFilters, setShipmentFilters] = useState<MetricsFilters>({
    bucket: "daily",
  });

  // ─── SLA Metrics State ─────────────────────────────────────────────────
  const [slaMetrics, setSlaMetrics] = useState<MetricsResponse | null>(null);
  const [slaLoading, setSlaLoading] = useState(true);
  const [slaError, setSlaError] = useState("");
  const [slaFilters, setSlaFilters] = useState<MetricsFilters>({
    bucket: "daily",
  });

  // ─── Pipeline Health Fetch ─────────────────────────────────────────────

  const fetchMetrics = useCallback(async () => {
    try {
      const [ingestionData, indexingData, poisonData] = await Promise.all([
        getIngestionMonitoring(),
        getIndexingMonitoring(),
        getPoisonQueueMonitoring(),
      ]);
      // API returns { data: {...}, request_id: "..." } — extract the data field
      setIngestion((ingestionData as any).data ?? ingestionData);
      setIndexing((indexingData as any).data ?? indexingData);
      setPoisonQueue((poisonData as any).data ?? poisonData);
      setError("");
      const now = new Date();
      setLastUpdated(now);
      lastUpdatedRef.current = now;
    } catch (err) {
      // On polling failure, keep stale data and show "last updated" indicator
      setError(
        err instanceof Error ? err.message : "Failed to fetch monitoring data",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  // ─── Shipment Metrics Fetch ────────────────────────────────────────────

  const fetchShipmentMetrics = useCallback(async (filters: MetricsFilters) => {
    setShipmentLoading(true);
    try {
      const data = await getShipmentMetrics(filters);
      setShipmentMetrics(data);
      setShipmentError("");
    } catch (err) {
      setShipmentError(
        err instanceof Error
          ? err.message
          : "Failed to fetch shipment metrics",
      );
    } finally {
      setShipmentLoading(false);
    }
  }, []);

  // ─── SLA Metrics Fetch ─────────────────────────────────────────────────

  const fetchSlaMetrics = useCallback(async (filters: MetricsFilters) => {
    setSlaLoading(true);
    try {
      const response = await getSlaMetrics(filters);
      setSlaMetrics(response);
      setSlaError("");
    } catch (err) {
      setSlaError(
        err instanceof Error ? err.message : "Failed to fetch SLA metrics",
      );
    } finally {
      setSlaLoading(false);
    }
  }, []);

  // ─── Initial fetch + auto-refresh every 30 seconds ─────────────────────

  useEffect(() => {
    fetchMetrics();
    const interval = setInterval(fetchMetrics, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [fetchMetrics]);

  // Fetch shipment metrics on mount and when filters change
  useEffect(() => {
    fetchShipmentMetrics(shipmentFilters);
  }, [shipmentFilters, fetchShipmentMetrics]);

  // Fetch SLA metrics on mount and when filters change
  useEffect(() => {
    fetchSlaMetrics(slaFilters);
  }, [slaFilters, fetchSlaMetrics]);

  // Update "seconds ago" counter every second when there's an error
  useEffect(() => {
    const tick = setInterval(() => {
      if (lastUpdatedRef.current) {
        setSecondsAgo(
          Math.floor(
            (Date.now() - lastUpdatedRef.current.getTime()) / 1000,
          ),
        );
      }
    }, 1000);
    return () => clearInterval(tick);
  }, []);

  // ─── Helpers for metric status ───────────────────────────────────────────

  function ingestionStatus(key: string, value: number): MetricStatus {
    const config = INGESTION_THRESHOLDS[key];
    return config ? getMetricStatus(value, config) : "healthy";
  }

  function indexingStatus(key: string, value: number): MetricStatus {
    const config = INDEXING_THRESHOLDS[key];
    return config ? getMetricStatus(value, config) : "healthy";
  }

  function poisonStatus(key: string, value: number): MetricStatus {
    const config = POISON_QUEUE_THRESHOLDS[key];
    return config ? getMetricStatus(value, config) : "healthy";
  }

  return (
    <div className="flex-1 flex flex-col h-full bg-gray-50">
      {/* Header */}
      <div className="px-6 pt-6 pb-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 mb-1">
            <div className="w-9 h-9 bg-gray-700 rounded-lg flex items-center justify-center">
              <Activity className="w-5 h-5 text-white" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-[#232323]">
                Ops Monitoring
              </h2>
              <p className="text-xs text-gray-500">
                Pipeline health — ingestion, indexing & poison queue
              </p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {error && lastUpdated && (
              <span className="text-xs text-yellow-600 bg-yellow-50 px-2 py-1 rounded">
                Last updated {secondsAgo}s ago
              </span>
            )}
            <button
              onClick={fetchMetrics}
              disabled={loading}
              className="p-2 rounded-lg text-gray-400 hover:text-[#232323] hover:bg-gray-100 transition-colors disabled:opacity-50"
              title="Refresh metrics"
            >
              <RefreshCw
                className={`w-4 h-4 ${loading ? "animate-spin" : ""}`}
              />
            </button>
          </div>
        </div>
      </div>

      {/* Error banner (only on initial load failure with no data) */}
      {error && !ingestion && !indexing && !poisonQueue && (
        <div className="mx-6 mb-4">
          <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
            {error}
          </p>
        </div>
      )}

      {/* Metric Cards Grid */}
      <div className="flex-1 min-h-0 overflow-auto px-6 pb-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Ingestion Card */}
          <MetricCard
            title="Ingestion Pipeline"
            icon={<Zap className="w-4 h-4 text-white" />}
            loading={loading}
          >
            {ingestion && (
              <>
                <MetricItem
                  label="Events Received"
                  value={(ingestion as any).events_received ?? 0}
                  status={ingestionStatus(
                    "events_received",
                    (ingestion as any).events_received ?? 0,
                  )}
                />
                <MetricItem
                  label="Events Processed"
                  value={(ingestion as any).events_processed ?? 0}
                  status={ingestionStatus(
                    "events_processed",
                    (ingestion as any).events_processed ?? 0,
                  )}
                />
                <MetricItem
                  label="Events Failed"
                  value={(ingestion as any).events_failed ?? 0}
                  status={ingestionStatus(
                    "events_failed",
                    (ingestion as any).events_failed ?? 0,
                  )}
                />
                <MetricItem
                  label="Avg Latency"
                  value={`${((ingestion as any).avg_latency_ms ?? (ingestion as any).avg_processing_latency_ms ?? 0).toFixed(1)} ms`}
                  status={ingestionStatus(
                    "avg_latency_ms",
                    (ingestion as any).avg_latency_ms ?? (ingestion as any).avg_processing_latency_ms ?? 0,
                  )}
                />
              </>
            )}
          </MetricCard>

          {/* Indexing Card */}
          <MetricCard
            title="Indexing Health"
            icon={<Database className="w-4 h-4 text-white" />}
            loading={loading}
          >
            {indexing && (
              <>
                <MetricItem
                  label="Documents Indexed"
                  value={(indexing as any).documents_indexed ?? (indexing as any).total_documents_indexed ?? 0}
                  status={indexingStatus(
                    "documents_indexed",
                    (indexing as any).documents_indexed ?? (indexing as any).total_documents_indexed ?? 0,
                  )}
                />
                <MetricItem
                  label="Indexing Errors"
                  value={(indexing as any).indexing_errors ?? 0}
                  status={indexingStatus(
                    "indexing_errors",
                    (indexing as any).indexing_errors ?? 0,
                  )}
                />
                <MetricItem
                  label="Bulk Success Rate"
                  value={`${(((indexing as any).bulk_success_rate ?? ((indexing as any).bulk_success_rate_pct != null ? (indexing as any).bulk_success_rate_pct / 100 : 1)) * 100).toFixed(1)}%`}
                  status={indexingStatus(
                    "bulk_success_rate",
                    (indexing as any).bulk_success_rate ?? ((indexing as any).bulk_success_rate_pct != null ? (indexing as any).bulk_success_rate_pct / 100 : 1),
                  )}
                />
                <MetricItem
                  label="Avg Latency"
                  value={`${((indexing as any).avg_latency_ms ?? (indexing as any).avg_indexing_latency_ms ?? 0).toFixed(1)} ms`}
                  status={indexingStatus(
                    "avg_latency_ms",
                    (indexing as any).avg_latency_ms ?? (indexing as any).avg_indexing_latency_ms ?? 0,
                  )}
                />
              </>
            )}
          </MetricCard>

          {/* Poison Queue Card */}
          <MetricCard
            title="Poison Queue"
            icon={<Skull className="w-4 h-4 text-white" />}
            loading={loading}
          >
            {poisonQueue && (
              <>
                <MetricItem
                  label="Queue Depth"
                  value={(poisonQueue as any).queue_depth ?? 0}
                  status={poisonStatus(
                    "queue_depth",
                    (poisonQueue as any).queue_depth ?? 0,
                  )}
                />
                <MetricItem
                  label="Oldest Event Age"
                  value={`${((poisonQueue as any).oldest_event_age_seconds ?? 0).toLocaleString()}s`}
                  status={poisonStatus(
                    "oldest_event_age_seconds",
                    (poisonQueue as any).oldest_event_age_seconds ?? 0,
                  )}
                />
                <MetricItem
                  label="Pending Count"
                  value={(poisonQueue as any).pending_count ?? (poisonQueue as any).status_breakdown?.pending ?? 0}
                  status={poisonStatus(
                    "pending_count",
                    (poisonQueue as any).pending_count ?? (poisonQueue as any).status_breakdown?.pending ?? 0,
                  )}
                />
                <MetricItem
                  label="Permanently Failed"
                  value={(poisonQueue as any).permanently_failed_count ?? (poisonQueue as any).status_breakdown?.permanently_failed ?? 0}
                  status={poisonStatus(
                    "permanently_failed_count",
                    (poisonQueue as any).permanently_failed_count ?? (poisonQueue as any).status_breakdown?.permanently_failed ?? 0,
                  )}
                />
              </>
            )}
          </MetricCard>
        </div>

        {/* Shipment Metrics Section */}
        <div className="mt-6">
          <ShipmentMetricsSection
            data={shipmentMetrics}
            loading={shipmentLoading}
            error={shipmentError}
            filters={shipmentFilters}
            onFiltersChange={setShipmentFilters}
          />
        </div>

        {/* SLA Metrics Section */}
        <div className="mt-6">
          <SlaMetricsSection
            data={slaMetrics}
            loading={slaLoading}
            error={slaError}
            filters={slaFilters}
            onFiltersChange={setSlaFilters}
          />
        </div>
      </div>
    </div>
  );
}
