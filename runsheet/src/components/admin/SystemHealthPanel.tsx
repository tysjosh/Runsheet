"use client";

/**
 * Settings → System health (UI revamp task 3.9, decision D12).
 *
 * The useful remainder of the retired Analytics → Ops Monitoring tab: the
 * platform-wide poison queue (depth, oldest event age, pending and
 * permanently failed). `/ops/monitoring/poison-queue` is `platform_admin`
 * only (task 0.2), and so is this section (`system-health` module). The
 * Elasticsearch-era ingestion and indexing cards are gone with their routes.
 *
 * Load errors use `LoadErrorState` with Retry; a poll refreshes every 30 s
 * and keeps the last good figures (with their age) when a refresh fails.
 */
import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { number, relative } from "../../lib/format";
import { classifyLoadError } from "../../services/apiErrors";
import type { PoisonQueueMetrics } from "../../services/opsApi";
import { getPoisonQueueMonitoring } from "../../services/opsApi";
import type { StatusKey } from "../../styles/tokens";
import {
  IconButton,
  LoadErrorState,
  Skeleton,
  StatusBadge,
  Toolbar,
} from "../ui";

export type MetricStatus = "healthy" | "degraded" | "critical";

export interface ThresholdConfig {
  /** "above": higher is worse (depth, counts); "below": lower is worse. */
  direction: "above" | "below";
  warning: number;
  critical: number;
}

/** Grade a value against its thresholds. */
export function getMetricStatus(
  value: number,
  config: ThresholdConfig,
): MetricStatus {
  if (config.direction === "above") {
    if (value > config.critical) return "critical";
    if (value > config.warning) return "degraded";
    return "healthy";
  }
  if (value < config.critical) return "critical";
  if (value < config.warning) return "degraded";
  return "healthy";
}

const QUEUE_DEPTH: ThresholdConfig = {
  direction: "above",
  warning: 50,
  critical: 100,
};

const BADGE: Record<MetricStatus, { status: StatusKey; label: string }> = {
  healthy: { status: "ok", label: "Healthy" },
  degraded: { status: "warning", label: "Degraded" },
  critical: { status: "critical", label: "Critical" },
};

export const REFRESH_INTERVAL_MS = 30_000;

type Raw = Partial<PoisonQueueMetrics> & {
  status_breakdown?: { pending?: number; permanently_failed?: number };
};

/** The route answers `{data: {...}}`; older builds sent the bare object. */
function unwrap(body: unknown): Raw {
  const b = body as { data?: Raw } & Raw;
  return (b?.data ?? b) as Raw;
}

export default function SystemHealthPanel() {
  const [metrics, setMetrics] = useState<Raw | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const body = await getPoisonQueueMonitoring();
      setMetrics(unwrap(body));
      setError(null);
      setUpdatedAt(new Date());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [load]);

  const depth = metrics?.queue_depth ?? 0;
  const pending =
    metrics?.pending_count ?? metrics?.status_breakdown?.pending ?? 0;
  const failed =
    metrics?.permanently_failed_count ??
    metrics?.status_breakdown?.permanently_failed ??
    0;
  const age = metrics?.oldest_event_age_seconds ?? 0;
  const grade = BADGE[getMetricStatus(depth, QUEUE_DEPTH)];

  const rows: { label: string; value: string }[] = [
    { label: "Queue depth", value: number(depth) },
    { label: "Oldest event age", value: `${number(age)} s` },
    { label: "Pending", value: number(pending) },
    { label: "Permanently failed", value: number(failed) },
  ];

  return (
    <div className="flex h-full flex-col">
      <Toolbar
        label="System health"
        search={
          <span className="text-sm font-semibold text-text">Poison queue</span>
        }
        filters={
          metrics ? (
            <StatusBadge status={grade.status} label={grade.label} />
          ) : undefined
        }
        end={
          <>
            {updatedAt && (
              <span className="whitespace-nowrap text-xs text-text-muted">
                Updated {relative(updatedAt)}
              </span>
            )}
            <IconButton
              label="Refresh"
              size="sm"
              onClick={() => load()}
              disabled={loading}
              icon={
                <RefreshCw
                  className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
                />
              }
            />
          </>
        }
      />
      <div className="p-4">
        {error && !metrics ? (
          <LoadErrorState
            failure={classifyLoadError(error, "Couldn't load system health.")}
            entityLabel="System health"
            onRetry={load}
            staffOnly
            embedded
          />
        ) : !metrics ? (
          <Skeleton rows={4} label="Loading system health" />
        ) : (
          <>
            {error && (
              <p role="status" className="mb-2 text-xs text-amber-800">
                Couldn't refresh. Showing figures from{" "}
                {updatedAt ? relative(updatedAt) : "earlier"}.
              </p>
            )}
            <dl className="grid max-w-xl grid-cols-2 gap-x-6 gap-y-2 rounded-lg border border-slate-200 bg-surface p-4 text-sm">
              {rows.map((r) => (
                <div key={r.label} className="contents">
                  <dt className="text-text-muted">{r.label}</dt>
                  <dd className="text-right font-semibold tabular-nums text-text">
                    {r.value}
                  </dd>
                </div>
              ))}
            </dl>
          </>
        )}
      </div>
    </div>
  );
}
