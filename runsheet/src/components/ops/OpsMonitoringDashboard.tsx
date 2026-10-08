"use client";

/**
 * Ops Monitoring Dashboard — poison queue health.
 *
 * Shown only to `platform_admin` (see `AnalyticsHub.tsx`): the poison queue is
 * platform-wide and `/ops/monitoring/poison-queue` is `platform_admin` only.
 * Full relegation to Settings → System health is UI revamp task 3.9.
 *
 * The Ingestion and Indexing cards were removed with their endpoints (UI revamp
 * task 0.2): both queried Elasticsearch indices dropped by migration 0007, so
 * ingestion always 500'd and indexing reported a fake 100% success rate.
 *
 * Cards load with `Promise.allSettled` and keep their own error, so one failing
 * card never blanks the others.
 *
 * Validates:
 * - Requirement 6.3: Display poison queue metrics via getPoisonQueueMonitoring
 * - Requirement 6.4: Color-code metric values green/yellow/red based on thresholds
 * - Requirement 6.5: Visual alert indicator next to metrics exceeding critical thresholds
 * - Requirement 6.6: Auto-refresh every 30 seconds with polling interval
 */

import {
  Activity,
  AlertTriangle,
  Loader2,
  RefreshCw,
  Skull,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { PoisonQueueMetrics } from "../../services/opsApi";
import { getPoisonQueueMonitoring } from "../../services/opsApi";

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

const POISON_QUEUE_THRESHOLDS: Record<string, ThresholdConfig> = {
  queue_depth: { direction: "above", warning: 50, critical: 100 },
};

// ─── Status Styling ──────────────────────────────────────────────────────────

const STATUS_STYLES: Record<MetricStatus, { text: string; bg: string }> = {
  healthy: { text: "text-success", bg: "bg-success-light" },
  degraded: { text: "text-warning", bg: "bg-warning-light" },
  critical: { text: "text-error", bg: "bg-error-light" },
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
          <AlertTriangle
            className="w-3.5 h-3.5 text-error"
            aria-label="Critical alert"
          />
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
  /** This card's own load error; other cards are unaffected. */
  error?: string;
}

function MetricCard({
  title,
  icon,
  children,
  loading,
  error,
}: MetricCardProps) {
  return (
    <div className="bg-white border border-gray-200 rounded-lg">
      <div className="flex items-center gap-2 px-5 py-4 border-b border-gray-100">
        <div className="w-8 h-8 bg-primary rounded-lg flex items-center justify-center">
          {icon}
        </div>
        <h3 className="text-sm font-semibold text-primary">{title}</h3>
      </div>
      <div className="px-5 py-4">
        {error && (
          <p
            role="alert"
            className="text-sm text-error bg-error-light px-3 py-2 rounded mb-2"
          >
            {error}
          </p>
        )}
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 text-gray-500 animate-spin" />
          </div>
        ) : (
          <div className="divide-y divide-gray-50">{children}</div>
        )}
      </div>
    </div>
  );
}

/** Message for a failed card; a network failure gets plain copy. */
function cardErrorMessage(reason: unknown, title: string): string {
  const message = reason instanceof Error ? reason.message : "";
  if (!message || message === "Failed to fetch") {
    return `Couldn't load ${title.toLowerCase()} metrics. Retrying in 30 seconds.`;
  }
  return message;
}

// ─── Main Dashboard Component ────────────────────────────────────────────────

export default function OpsMonitoringDashboard() {
  const [poisonQueue, setPoisonQueue] = useState<PoisonQueueMetrics | null>(
    null,
  );
  const [poisonQueueError, setPoisonQueueError] = useState("");
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [secondsAgo, setSecondsAgo] = useState(0);
  const lastUpdatedRef = useRef<Date | null>(null);

  // ─── Pipeline Health Fetch ─────────────────────────────────────────────

  const fetchMetrics = useCallback(async () => {
    // allSettled so each card succeeds or fails on its own (more cards may
    // join this list when the page moves to Settings → System health).
    const [poisonResult] = await Promise.allSettled([
      getPoisonQueueMonitoring(),
    ]);
    if (poisonResult.status === "fulfilled") {
      // API returns { data: {...}, request_id: "..." } — extract the data field
      const poisonData = poisonResult.value;
      setPoisonQueue((poisonData as any).data ?? poisonData);
      setPoisonQueueError("");
      const now = new Date();
      setLastUpdated(now);
      lastUpdatedRef.current = now;
    } else {
      // On polling failure, keep stale data and show "last updated" indicator
      setPoisonQueueError(
        cardErrorMessage(poisonResult.reason, "Poison Queue"),
      );
    }
    setLoading(false);
  }, []);

  // ─── Initial fetch + auto-refresh every 30 seconds ─────────────────────

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
          Math.floor((Date.now() - lastUpdatedRef.current.getTime()) / 1000),
        );
      }
    }, 1000);
    return () => clearInterval(tick);
  }, []);

  // ─── Helpers for metric status ───────────────────────────────────────────

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
              <h2 className="text-lg font-semibold text-primary">
                Ops Monitoring
              </h2>
              <p className="text-xs text-gray-500">
                Pipeline health — poison queue
              </p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {poisonQueueError && lastUpdated && (
              <span className="text-xs text-warning bg-warning-light px-2 py-1 rounded">
                Last updated {secondsAgo}s ago
              </span>
            )}
            <button
              onClick={fetchMetrics}
              disabled={loading}
              className="p-2 rounded-lg text-gray-500 hover:text-primary hover:bg-gray-100 transition-colors disabled:opacity-50"
              title="Refresh metrics"
              aria-label="Refresh metrics"
            >
              <RefreshCw
                className={`w-4 h-4 ${loading ? "animate-spin" : ""}`}
              />
            </button>
          </div>
        </div>
      </div>

      {/* Metric Cards Grid */}
      <div className="flex-1 min-h-0 overflow-auto px-6 pb-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Poison Queue Card */}
          <MetricCard
            title="Poison Queue"
            icon={<Skull className="w-4 h-4 text-white" />}
            loading={loading}
            error={poisonQueueError}
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
                  value={
                    (poisonQueue as any).pending_count ??
                    (poisonQueue as any).status_breakdown?.pending ??
                    0
                  }
                  status={poisonStatus(
                    "pending_count",
                    (poisonQueue as any).pending_count ??
                      (poisonQueue as any).status_breakdown?.pending ??
                      0,
                  )}
                />
                <MetricItem
                  label="Permanently Failed"
                  value={
                    (poisonQueue as any).permanently_failed_count ??
                    (poisonQueue as any).status_breakdown?.permanently_failed ??
                    0
                  }
                  status={poisonStatus(
                    "permanently_failed_count",
                    (poisonQueue as any).permanently_failed_count ??
                      (poisonQueue as any).status_breakdown
                        ?.permanently_failed ??
                      0,
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
