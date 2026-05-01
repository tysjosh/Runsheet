"use client";

/**
 * Scheduling Metrics Analytics page — Job Metrics, Completion Rates,
 * Asset Utilization & Delay Statistics.
 *
 * Four-section dashboard with shared time range filters (bucket, start_date,
 * end_date). All sections re-fetch when the time range changes.
 *
 * Validates:
 * - Requirement 10.1: Display job count metrics by status and type in time buckets via getJobMetrics
 * - Requirement 10.2: Display completion rate metrics (completion_rate, avg_completion_minutes per job_type) via getCompletionMetrics
 * - Requirement 10.3: Display asset utilization metrics (total_jobs, active_jobs, idle_hours per asset) via getAssetUtilization
 * - Requirement 10.4: Display delay statistics (total_delayed, avg_delay_minutes, delays_by_type) via getDelayMetrics
 * - Requirement 10.5: Provide time range filters (bucket granularity, start_date, end_date) that apply to all four sections
 * - Requirement 10.6: Re-fetch all metrics when time range changes
 */

import {
  AlertTriangle,
  BarChart3,
  Calendar,
  CheckCircle,
  Clock,
  Loader2,
  RefreshCw,
  TrendingUp,
  Truck,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type {
  AssetUtilizationMetric,
  CompletionMetric,
  DelayMetrics,
  JobMetricsBucket,
  MetricsFilters,
} from "../../services/schedulingApi";
import {
  getAssetUtilization,
  getCompletionMetrics,
  getDelayMetrics,
  getJobMetrics,
} from "../../services/schedulingApi";

// ─── Section Card ────────────────────────────────────────────────────────────

interface SectionCardProps {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  loading: boolean;
}

function SectionCard({ title, icon, children, loading }: SectionCardProps) {
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
          children
        )}
      </div>
    </div>
  );
}

// ─── Summary Stat Card ───────────────────────────────────────────────────────

interface StatCardProps {
  label: string;
  value: string | number;
  icon: React.ReactNode;
  accent?: "red" | "yellow" | "blue" | "green";
}

function StatCard({ label, value, icon, accent = "blue" }: StatCardProps) {
  const accentStyles: Record<string, { bg: string; text: string; iconBg: string }> = {
    red: { bg: "bg-red-50", text: "text-red-700", iconBg: "bg-red-100" },
    yellow: { bg: "bg-yellow-50", text: "text-yellow-700", iconBg: "bg-yellow-100" },
    blue: { bg: "bg-blue-50", text: "text-blue-700", iconBg: "bg-blue-100" },
    green: { bg: "bg-green-50", text: "text-green-700", iconBg: "bg-green-100" },
  };
  const style = accentStyles[accent];

  return (
    <div className={`rounded-lg p-4 ${style.bg}`}>
      <div className="flex items-center gap-2 mb-2">
        <div className={`w-7 h-7 ${style.iconBg} rounded-md flex items-center justify-center`}>
          {icon}
        </div>
        <span className="text-xs text-gray-500">{label}</span>
      </div>
      <p className={`text-xl font-bold ${style.text}`}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
    </div>
  );
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function SchedulingMetricsPage() {
  // ── Shared time range filters ────────────────────────────────────────────
  const [bucket, setBucket] = useState<"hourly" | "daily">("daily");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  // ── Section data ─────────────────────────────────────────────────────────
  const [jobMetrics, setJobMetrics] = useState<JobMetricsBucket[]>([]);
  const [completionMetrics, setCompletionMetrics] = useState<CompletionMetric[]>([]);
  const [assetUtilization, setAssetUtilization] = useState<AssetUtilizationMetric[]>([]);
  const [delayMetrics, setDelayMetrics] = useState<DelayMetrics | null>(null);

  // ── Loading & error state ────────────────────────────────────────────────
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // ── Build filters from current state ─────────────────────────────────────
  const buildFilters = useCallback((): MetricsFilters => {
    const filters: MetricsFilters = { bucket };
    if (startDate) filters.start_date = startDate;
    if (endDate) filters.end_date = endDate;
    return filters;
  }, [bucket, startDate, endDate]);

  // ── Fetch all metrics ────────────────────────────────────────────────────
  const fetchAllMetrics = useCallback(async () => {
    setLoading(true);
    setError("");
    const filters = buildFilters();

    try {
      const [jobRes, completionRes, assetRes, delayRes] = await Promise.allSettled([
        getJobMetrics(filters),
        getCompletionMetrics(filters),
        getAssetUtilization(filters),
        getDelayMetrics(filters),
      ]);

      if (jobRes.status === "fulfilled") setJobMetrics((jobRes.value as any).data ?? []);
      if (completionRes.status === "fulfilled") setCompletionMetrics((completionRes.value as any).data ?? []);
      if (assetRes.status === "fulfilled") setAssetUtilization((assetRes.value as any).data ?? []);
      if (delayRes.status === "fulfilled") setDelayMetrics((delayRes.value as any).data ?? null);

      const failed = [jobRes, completionRes, assetRes, delayRes].filter(r => r.status === "rejected");
      if (failed.length > 0 && failed.length < 4) {
        setError(`Some metrics failed to load (${failed.length}/4)`);
      } else if (failed.length === 4) {
        setError("Failed to fetch scheduling metrics");
      }
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to fetch scheduling metrics",
      );
    } finally {
      setLoading(false);
    }
  }, [buildFilters]);

  // ── Re-fetch when filters change ─────────────────────────────────────────
  useEffect(() => {
    fetchAllMetrics();
  }, [fetchAllMetrics]);

  return (
    <div className="flex-1 flex flex-col h-full bg-gray-50">
      {/* Header */}
      <div className="px-6 pt-6 pb-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 mb-1">
            <div className="w-9 h-9 bg-gray-700 rounded-lg flex items-center justify-center">
              <TrendingUp className="w-5 h-5 text-white" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-[#232323]">
                Scheduling Metrics
              </h2>
              <p className="text-xs text-gray-500">
                Job counts, completion rates, asset utilization & delay statistics
              </p>
            </div>
          </div>
          <button
            onClick={fetchAllMetrics}
            disabled={loading}
            className="p-2 rounded-lg text-gray-400 hover:text-[#232323] hover:bg-gray-100 transition-colors disabled:opacity-50"
            title="Refresh metrics"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {/* Time Range Filters */}
      <div className="px-6 pb-4">
        <div className="flex flex-wrap items-center gap-4 bg-white border border-gray-200 rounded-lg px-4 py-3">
          <div className="flex items-center gap-2">
            <Calendar className="w-4 h-4 text-gray-400" />
            <span className="text-xs font-medium text-gray-500">Filters</span>
          </div>

          {/* Bucket granularity */}
          <div className="flex items-center gap-2">
            <label htmlFor="bucket-select" className="text-xs text-gray-500">
              Granularity
            </label>
            <select
              id="bucket-select"
              value={bucket}
              onChange={(e) => setBucket(e.target.value as "hourly" | "daily")}
              className="text-sm border border-gray-200 rounded-md px-2 py-1 bg-white text-[#232323] focus:outline-none focus:ring-1 focus:ring-gray-300"
            >
              <option value="hourly">Hourly</option>
              <option value="daily">Daily</option>
            </select>
          </div>

          {/* Start date */}
          <div className="flex items-center gap-2">
            <label htmlFor="start-date" className="text-xs text-gray-500">
              Start
            </label>
            <input
              id="start-date"
              type="date"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="text-sm border border-gray-200 rounded-md px-2 py-1 bg-white text-[#232323] focus:outline-none focus:ring-1 focus:ring-gray-300"
            />
          </div>

          {/* End date */}
          <div className="flex items-center gap-2">
            <label htmlFor="end-date" className="text-xs text-gray-500">
              End
            </label>
            <input
              id="end-date"
              type="date"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              className="text-sm border border-gray-200 rounded-md px-2 py-1 bg-white text-[#232323] focus:outline-none focus:ring-1 focus:ring-gray-300"
            />
          </div>

          {/* Clear filters */}
          {(startDate || endDate) && (
            <button
              onClick={() => {
                setStartDate("");
                setEndDate("");
              }}
              className="text-xs text-gray-400 hover:text-gray-600 transition-colors"
            >
              Clear dates
            </button>
          )}
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div className="mx-6 mb-4">
          <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
            {error}
          </p>
        </div>
      )}

      {/* Metrics Sections */}
      <div className="flex-1 min-h-0 overflow-auto px-6 pb-6 space-y-6">
        {/* ── Job Metrics Section ─────────────────────────────────────────── */}
        <SectionCard
          title="Job Metrics"
          icon={<BarChart3 className="w-4 h-4 text-white" />}
          loading={loading}
        >
          {jobMetrics.length === 0 ? (
            <p className="text-sm text-gray-400 py-4 text-center">
              No job metrics data for the selected time range.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-100">
                    <th className="text-left py-2 pr-4 text-xs font-medium text-gray-500">
                      Timestamp
                    </th>
                    <th className="text-left py-2 pr-4 text-xs font-medium text-gray-500">
                      Counts by Status
                    </th>
                    <th className="text-left py-2 text-xs font-medium text-gray-500">
                      Counts by Type
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {jobMetrics.map((bucket, idx) => (
                    <tr key={idx}>
                      <td className="py-2 pr-4 text-gray-700 whitespace-nowrap">
                        {new Date(bucket.timestamp).toLocaleString()}
                      </td>
                      <td className="py-2 pr-4">
                        <div className="flex flex-wrap gap-1.5">
                          {Object.entries(bucket.counts_by_status).map(
                            ([status, count]) => (
                              <span
                                key={status}
                                className="inline-flex items-center gap-1 text-xs bg-gray-100 text-gray-700 px-2 py-0.5 rounded"
                              >
                                <span className="font-medium">{status}:</span>{" "}
                                {count}
                              </span>
                            ),
                          )}
                        </div>
                      </td>
                      <td className="py-2">
                        <div className="flex flex-wrap gap-1.5">
                          {Object.entries(bucket.counts_by_type).map(
                            ([type, count]) => (
                              <span
                                key={type}
                                className="inline-flex items-center gap-1 text-xs bg-blue-50 text-blue-700 px-2 py-0.5 rounded"
                              >
                                <span className="font-medium">{type}:</span>{" "}
                                {count}
                              </span>
                            ),
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </SectionCard>

        {/* ── Completion Rates Section ────────────────────────────────────── */}
        <SectionCard
          title="Completion Rates"
          icon={<CheckCircle className="w-4 h-4 text-white" />}
          loading={loading}
        >
          {completionMetrics.length === 0 ? (
            <p className="text-sm text-gray-400 py-4 text-center">
              No completion data for the selected time range.
            </p>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {completionMetrics.map((metric) => (
                <div
                  key={metric.job_type}
                  className="border border-gray-100 rounded-lg p-4"
                >
                  <p className="text-xs font-medium text-gray-500 mb-3 uppercase tracking-wide">
                    {metric.job_type}
                  </p>
                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-gray-500">
                        Completion Rate
                      </span>
                      <span className="text-sm font-semibold text-[#232323]">
                        {(metric.completion_rate * 100).toFixed(1)}%
                      </span>
                    </div>
                    <div className="w-full bg-gray-100 rounded-full h-1.5">
                      <div
                        className="bg-green-500 h-1.5 rounded-full transition-all"
                        style={{
                          width: `${Math.min(metric.completion_rate * 100, 100)}%`,
                        }}
                      />
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-gray-500">
                        Avg Completion
                      </span>
                      <span className="text-sm font-semibold text-[#232323]">
                        {metric.avg_completion_minutes.toFixed(1)} min
                      </span>
                    </div>
                    <div className="flex items-center justify-between text-xs text-gray-400">
                      <span>
                        {metric.completed} / {metric.total} jobs
                      </span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </SectionCard>

        {/* ── Asset Utilization Section ───────────────────────────────────── */}
        <SectionCard
          title="Asset Utilization"
          icon={<Truck className="w-4 h-4 text-white" />}
          loading={loading}
        >
          {assetUtilization.length === 0 ? (
            <p className="text-sm text-gray-400 py-4 text-center">
              No asset utilization data for the selected time range.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-100">
                    <th className="text-left py-2 pr-4 text-xs font-medium text-gray-500">
                      Asset
                    </th>
                    <th className="text-left py-2 pr-4 text-xs font-medium text-gray-500">
                      Type
                    </th>
                    <th className="text-right py-2 pr-4 text-xs font-medium text-gray-500">
                      Total Jobs
                    </th>
                    <th className="text-right py-2 pr-4 text-xs font-medium text-gray-500">
                      Active Jobs
                    </th>
                    <th className="text-right py-2 pr-4 text-xs font-medium text-gray-500">
                      Completed
                    </th>
                    <th className="text-right py-2 pr-4 text-xs font-medium text-gray-500">
                      Active Hours
                    </th>
                    <th className="text-right py-2 text-xs font-medium text-gray-500">
                      Idle Hours
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {assetUtilization.map((asset) => (
                    <tr key={asset.asset_id}>
                      <td className="py-2 pr-4 text-gray-700 font-medium">
                        {asset.asset_id}
                      </td>
                      <td className="py-2 pr-4">
                        <span className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded">
                          {asset.asset_type}
                        </span>
                      </td>
                      <td className="py-2 pr-4 text-right text-gray-700">
                        {asset.total_jobs}
                      </td>
                      <td className="py-2 pr-4 text-right text-gray-700">
                        {asset.active_jobs}
                      </td>
                      <td className="py-2 pr-4 text-right text-gray-700">
                        {asset.completed_jobs}
                      </td>
                      <td className="py-2 pr-4 text-right text-gray-700">
                        {asset.total_active_hours.toFixed(1)}
                      </td>
                      <td className="py-2 text-right text-gray-700">
                        {asset.idle_hours.toFixed(1)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </SectionCard>

        {/* ── Delay Statistics Section ────────────────────────────────────── */}
        <SectionCard
          title="Delay Statistics"
          icon={<Clock className="w-4 h-4 text-white" />}
          loading={loading}
        >
          {!delayMetrics ? (
            <p className="text-sm text-gray-400 py-4 text-center">
              No delay data for the selected time range.
            </p>
          ) : (
            <div className="space-y-4">
              {/* Summary cards */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <StatCard
                  label="Total Delayed"
                  value={(delayMetrics as any).total_delayed ?? 0}
                  icon={<AlertTriangle className="w-3.5 h-3.5 text-red-500" />}
                  accent="red"
                />
                <StatCard
                  label="Avg Delay"
                  value={`${((delayMetrics as any).avg_delay_minutes ?? 0).toFixed(1)} min`}
                  icon={<Clock className="w-3.5 h-3.5 text-yellow-500" />}
                  accent="yellow"
                />
              </div>

              {/* Delays by type breakdown */}
              {delayMetrics.delays_by_type && Object.keys(delayMetrics.delays_by_type).length > 0 && (
                <div>
                  <p className="text-xs font-medium text-gray-500 mb-2">
                    Delays by Type
                  </p>
                  <div className="space-y-1.5">
                    {Object.entries(delayMetrics.delays_by_type).map(
                      ([type, count]) => (
                        <div
                          key={type}
                          className="flex items-center justify-between py-1.5"
                        >
                          <span className="text-sm text-gray-600">{type}</span>
                          <span className="text-sm font-semibold text-[#232323] bg-gray-100 px-2 py-0.5 rounded">
                            {count}
                          </span>
                        </div>
                      ),
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
        </SectionCard>
      </div>
    </div>
  );
}
