"use client";

import { AlertTriangle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import LoadingSpinner from "../../../components/LoadingSpinner";
import FailureBarChart from "../../../components/ops/FailureBarChart";
import FailureTrendChart from "../../../components/ops/FailureTrendChart";
import type {
  FailureFilters,
  MetricsBucket,
  MetricsBucketEntry,
  MetricsFilters,
  OpsShipment,
} from "../../../services/opsApi";
import {
  getFailureMetrics,
  getShipmentFailures,
} from "../../../services/opsApi";

// ─── Time Range Helpers ──────────────────────────────────────────────────────

type TimeRangePreset = "today" | "7d" | "30d" | "custom";

interface TimeRange {
  preset: TimeRangePreset;
  start_date: string;
  end_date: string;
}

function toISODate(date: Date): string {
  return date.toISOString().split("T")[0];
}

function getPresetDates(preset: TimeRangePreset): {
  start_date: string;
  end_date: string;
} {
  const now = new Date();
  const end = toISODate(now);

  switch (preset) {
    case "today":
      return { start_date: end, end_date: end };
    case "7d": {
      const start = new Date(now);
      start.setDate(start.getDate() - 7);
      return { start_date: toISODate(start), end_date: end };
    }
    case "30d": {
      const start = new Date(now);
      start.setDate(start.getDate() - 30);
      return { start_date: toISODate(start), end_date: end };
    }
    default:
      return { start_date: "", end_date: "" };
  }
}

function getBucketForPreset(preset: TimeRangePreset): MetricsBucket {
  return preset === "today" ? "hourly" : "daily";
}

// ─── Page Component ──────────────────────────────────────────────────────────

/**
 * Failure Analytics page.
 *
 * Displays failure counts by reason (bar chart), failure trend over time,
 * and a table of recent failed shipments. Supports time range selection
 * and click-to-filter on the bar chart.
 *
 * Validates: Requirements 14.1-14.5
 */
export default function OpsFailureAnalyticsPage() {
  const [metrics, setMetrics] = useState<MetricsBucketEntry[]>([]);
  const [failures, setFailures] = useState<OpsShipment[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedReason, setSelectedReason] = useState("");
  const [timeRange, setTimeRange] = useState<TimeRange>(() => {
    const dates = getPresetDates("7d");
    return { preset: "7d", ...dates };
  });

  const loadData = useCallback(async () => {
    try {
      setLoading(true);

      const metricsFilters: MetricsFilters = {
        bucket: getBucketForPreset(timeRange.preset),
      };
      if (timeRange.start_date)
        metricsFilters.start_date = timeRange.start_date;
      if (timeRange.end_date) metricsFilters.end_date = timeRange.end_date;

      const failureFilters: FailureFilters = {};
      if (timeRange.start_date)
        failureFilters.start_date = timeRange.start_date;
      if (timeRange.end_date) failureFilters.end_date = timeRange.end_date;

      const [metricsRes, failuresRes] = await Promise.all([
        getFailureMetrics(metricsFilters),
        getShipmentFailures(failureFilters),
      ]);

      setMetrics(metricsRes.data);
      setFailures(failuresRes.data);
    } catch (error) {
      console.error("Failed to load failure analytics:", error);
    } finally {
      setLoading(false);
    }
  }, [timeRange]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  /** Handle preset time range selection. Validates: Requirement 14.4 */
  const handlePresetChange = (preset: TimeRangePreset) => {
    if (preset === "custom") {
      setTimeRange((prev) => ({ ...prev, preset: "custom" }));
    } else {
      const dates = getPresetDates(preset);
      setTimeRange({ preset, ...dates });
      setSelectedReason("");
    }
  };

  /** Handle custom date changes */
  const handleCustomDate = (
    field: "start_date" | "end_date",
    value: string,
  ) => {
    setTimeRange((prev) => ({ ...prev, preset: "custom", [field]: value }));
  };

  /**
   * Handle click on a failure reason in the bar chart.
   * Filters the table to show only shipments with that reason.
   *
   * Validates: Requirement 14.5
   */
  const handleReasonClick = (reason: string) => {
    setSelectedReason(reason);
  };

  // Filter table rows by selected reason
  const filteredFailures = selectedReason
    ? failures.filter((s) => s.failure_reason === selectedReason)
    : failures;

  if (loading) {
    return <LoadingSpinner message="Loading failure analytics..." />;
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
            <AlertTriangle className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-[#232323]">
              Failure Analytics
            </h1>
            <p className="text-gray-500">Analyze failure reasons and trends</p>
          </div>
        </div>

        {/* Time Range Controls — Validates: Requirement 14.4 */}
        <TimeRangeSelector
          timeRange={timeRange}
          onPresetChange={handlePresetChange}
          onCustomDate={handleCustomDate}
        />
      </div>

      {/* Charts */}
      <div className="border-b border-gray-100 px-8 py-6 grid grid-cols-1 lg:grid-cols-2 gap-8">
        <FailureBarChart
          data={metrics}
          selectedReason={selectedReason}
          onReasonClick={handleReasonClick}
        />
        <FailureTrendChart data={metrics} />
      </div>

      {/* Failed Shipments Table — Validates: Requirement 14.3 */}
      <div className="flex-1 overflow-y-auto">
        <FailedShipmentsTable
          shipments={filteredFailures}
          selectedReason={selectedReason}
          onClearFilter={() => setSelectedReason("")}
        />
      </div>
    </div>
  );
}

// ─── Sub-components ──────────────────────────────────────────────────────────

const PRESET_OPTIONS: { value: TimeRangePreset; label: string }[] = [
  { value: "today", label: "Today" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "custom", label: "Custom" },
];

function TimeRangeSelector({
  timeRange,
  onPresetChange,
  onCustomDate,
}: {
  timeRange: TimeRange;
  onPresetChange: (preset: TimeRangePreset) => void;
  onCustomDate: (field: "start_date" | "end_date", value: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      {PRESET_OPTIONS.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onPresetChange(opt.value)}
          className={`px-3 py-1.5 text-sm rounded-lg border transition-colors ${
            timeRange.preset === opt.value
              ? "bg-[#232323] text-white border-[#232323]"
              : "bg-white text-gray-600 border-gray-200 hover:border-gray-300"
          }`}
          aria-pressed={timeRange.preset === opt.value}
        >
          {opt.label}
        </button>
      ))}

      {timeRange.preset === "custom" && (
        <>
          <input
            type="date"
            value={timeRange.start_date}
            onChange={(e) => onCustomDate("start_date", e.target.value)}
            className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            aria-label="Custom start date"
          />
          <span className="text-gray-400 text-sm">to</span>
          <input
            type="date"
            value={timeRange.end_date}
            onChange={(e) => onCustomDate("end_date", e.target.value)}
            className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            aria-label="Custom end date"
          />
        </>
      )}
    </div>
  );
}

function FailedShipmentsTable({
  shipments,
  selectedReason,
  onClearFilter,
}: {
  shipments: OpsShipment[];
  selectedReason: string;
  onClearFilter: () => void;
}) {
  return (
    <div className="px-8 py-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-medium text-gray-700">
          Recent Failed Shipments
          {selectedReason && (
            <span className="ml-2 text-xs text-red-600 font-normal">
              filtered: {selectedReason}
            </span>
          )}
        </h3>
        {selectedReason && (
          <button
            type="button"
            onClick={onClearFilter}
            className="text-xs text-gray-500 hover:text-gray-700 underline"
          >
            Clear filter
          </button>
        )}
      </div>

      {shipments.length === 0 ? (
        <p className="text-sm text-gray-400 py-4">No failed shipments found</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100 text-left text-gray-500">
                <th className="py-2 pr-4 font-medium">Shipment ID</th>
                <th className="py-2 pr-4 font-medium">Failure Reason</th>
                <th className="py-2 pr-4 font-medium">Rider</th>
                <th className="py-2 font-medium">Timestamp</th>
              </tr>
            </thead>
            <tbody>
              {shipments.map((s) => (
                <tr
                  key={s.shipment_id}
                  className="border-b border-gray-50 hover:bg-gray-50 transition-colors"
                >
                  <td className="py-2.5 pr-4 font-mono text-xs text-gray-800">
                    {s.shipment_id}
                  </td>
                  <td className="py-2.5 pr-4">
                    <span className="inline-block px-2 py-0.5 text-xs rounded bg-red-50 text-red-700">
                      {s.failure_reason ?? "Unknown"}
                    </span>
                  </td>
                  <td className="py-2.5 pr-4 text-gray-600">
                    {s.rider_id ?? "—"}
                  </td>
                  <td className="py-2.5 text-gray-500 text-xs">
                    {s.updated_at
                      ? new Date(s.updated_at).toLocaleString()
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
