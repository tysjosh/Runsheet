"use client";

/**
 * Ops Monitoring Dashboard — Ingestion, Indexing & Poison Queue health.
 *
 * Three-card grid layout displaying pipeline health metrics with color-coded
 * values (green/yellow/red) and auto-refresh every 30 seconds.
 *
 * Validates:
 * - Requirement 6.1: Display ingestion metrics via getIngestionMonitoring
 * - Requirement 6.2: Display indexing metrics via getIndexingMonitoring
 * - Requirement 6.3: Display poison queue metrics via getPoisonQueueMonitoring
 * - Requirement 6.4: Color-code metric values green/yellow/red based on thresholds
 * - Requirement 6.5: Visual alert indicator next to metrics exceeding critical thresholds
 * - Requirement 6.6: Auto-refresh every 30 seconds with polling interval
 */

import {
  Activity,
  AlertTriangle,
  Database,
  Loader2,
  RefreshCw,
  Skull,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type {
  IngestionMetrics,
  IndexingMetrics,
  PoisonQueueMetrics,
} from "../../services/opsApi";
import {
  getIngestionMonitoring,
  getIndexingMonitoring,
  getPoisonQueueMonitoring,
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

  // Initial fetch + auto-refresh every 30 seconds
  useEffect(() => {
    fetchMetrics();
    const interval = setInterval(fetchMetrics, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [fetchMetrics]);

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
      </div>
    </div>
  );
}
