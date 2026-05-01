"use client";

import { AlertTriangle, ChevronDown, Download, X, MapPin, Clock, Package, User } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import LoadingSpinner from "../../../components/LoadingSpinner";
import FailureBarChart from "../../../components/ops/FailureBarChart";
import FailureTrendChart from "../../../components/ops/FailureTrendChart";
import type {
  FailureFilters,
  MetricsBucket,
  MetricsBucketEntry,
  MetricsFilters,
  OpsShipment,
  ShipmentDetail,
} from "../../../services/opsApi";
import {
  getFailureMetrics,
  getShipmentById,
  getShipmentFailures,
} from "../../../services/opsApi";
import { generateFailureCSV } from "./csvExport";

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
  const [knownReasons, setKnownReasons] = useState<string[]>([]);
  const [drillDownShipmentId, setDrillDownShipmentId] = useState<string | null>(null);
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
      if (selectedReason)
        metricsFilters.failure_reason = selectedReason;

      const failureFilters: FailureFilters = {};
      if (timeRange.start_date)
        failureFilters.start_date = timeRange.start_date;
      if (timeRange.end_date) failureFilters.end_date = timeRange.end_date;
      if (selectedReason)
        failureFilters.failure_reason = selectedReason;

      const [metricsRes, failuresRes] = await Promise.all([
        getFailureMetrics(metricsFilters),
        getShipmentFailures(failureFilters),
      ]);

      setMetrics(metricsRes.data);
      setFailures(failuresRes.data);

      // Extract known failure reasons from unfiltered metrics for the dropdown
      // Only update the list when no filter is active to keep the full set of options
      if (!selectedReason) {
        const reasons = new Set<string>();
        for (const bucket of metricsRes.data) {
          if (bucket.breakdown) {
            for (const reason of Object.keys(bucket.breakdown)) {
              reasons.add(reason);
            }
          }
        }
        setKnownReasons(Array.from(reasons).sort());
      }
    } catch (error) {
      console.error("Failed to load failure analytics:", error);
    } finally {
      setLoading(false);
    }
  }, [timeRange, selectedReason]);

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
   * Re-queries the API with the selected filter.
   *
   * Validates: Requirements 9.2, 9.3
   */
  const handleReasonClick = (reason: string) => {
    setSelectedReason(reason);
  };

  /** Handle failure type dropdown change. Validates: Requirements 9.2, 9.3 */
  const handleDropdownReasonChange = (reason: string) => {
    setSelectedReason(reason);
  };

  // Filter table rows by selected reason (client-side fallback for backends
  // that don't support the failure_reason query param)
  const filteredFailures = selectedReason
    ? failures.filter((s) => s.failure_reason === selectedReason)
    : failures;

  // Filter metrics data for charts when a reason is selected
  const filteredMetrics = useMemo(() => {
    if (!selectedReason) return metrics;
    return metrics.map((bucket) => {
      if (!bucket.breakdown) return { ...bucket, count: 0 };
      const reasonCount = bucket.breakdown[selectedReason] ?? 0;
      return {
        ...bucket,
        count: reasonCount,
        breakdown: { [selectedReason]: reasonCount },
      };
    });
  }, [metrics, selectedReason]);

  /** Trigger CSV download of the currently filtered failure data. Validates: Requirements 9.4, 9.5 */
  const handleExportCSV = () => {
    const csv = generateFailureCSV(filteredFailures);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "failure-analytics-export.csv";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

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
        <div className="flex flex-wrap items-center gap-4">
          <TimeRangeSelector
            timeRange={timeRange}
            onPresetChange={handlePresetChange}
            onCustomDate={handleCustomDate}
          />

          {/* Failure Type Filter — Validates: Requirements 9.2, 9.3 */}
          <FailureTypeDropdown
            reasons={knownReasons}
            selectedReason={selectedReason}
            onReasonChange={handleDropdownReasonChange}
          />

          {/* CSV Export — Validates: Requirements 9.4, 9.5 */}
          <button
            type="button"
            onClick={handleExportCSV}
            className="ml-auto px-3 py-1.5 text-sm rounded-lg border bg-white text-gray-600 border-gray-200 hover:border-gray-300 transition-colors flex items-center gap-1.5"
            aria-label="Export failure data as CSV"
          >
            <Download className="w-4 h-4" />
            Export
          </button>
        </div>
      </div>

      {/* Charts — filtered by selected failure type */}
      <div className="border-b border-gray-100 px-8 py-6 grid grid-cols-1 lg:grid-cols-2 gap-8">
        <FailureBarChart
          data={filteredMetrics}
          selectedReason={selectedReason}
          onReasonClick={handleReasonClick}
        />
        <FailureTrendChart data={filteredMetrics} />
      </div>

      {/* Failed Shipments Table — Validates: Requirement 14.3 */}
      <div className="flex-1 overflow-y-auto">
        <FailedShipmentsTable
          shipments={filteredFailures}
          selectedReason={selectedReason}
          onClearFilter={() => setSelectedReason("")}
          onShipmentClick={(shipmentId) => setDrillDownShipmentId(shipmentId)}
        />
      </div>

      {/* Drill-down Panel — Validates: Requirement 9.1 */}
      {drillDownShipmentId && (
        <ShipmentDrillDownPanel
          shipmentId={drillDownShipmentId}
          onClose={() => setDrillDownShipmentId(null)}
        />
      )}
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

/**
 * Dropdown filter for selecting a specific failure_reason.
 * Lists all known failure reasons extracted from the current metrics data.
 * Validates: Requirements 9.2, 9.3
 */
function FailureTypeDropdown({
  reasons,
  selectedReason,
  onReasonChange,
}: {
  reasons: string[];
  selectedReason: string;
  onReasonChange: (reason: string) => void;
}) {
  return (
    <div className="relative">
      <select
        value={selectedReason}
        onChange={(e) => onReasonChange(e.target.value)}
        className={`appearance-none pl-3 pr-8 py-1.5 text-sm rounded-lg border transition-colors cursor-pointer ${
          selectedReason
            ? "bg-red-50 text-red-700 border-red-200"
            : "bg-white text-gray-600 border-gray-200 hover:border-gray-300"
        }`}
        aria-label="Filter by failure type"
      >
        <option value="">All failure types</option>
        {reasons.map((reason) => (
          <option key={reason} value={reason}>
            {reason}
          </option>
        ))}
      </select>
      <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
    </div>
  );
}

function FailedShipmentsTable({
  shipments,
  selectedReason,
  onClearFilter,
  onShipmentClick,
}: {
  shipments: OpsShipment[];
  selectedReason: string;
  onClearFilter: () => void;
  onShipmentClick: (shipmentId: string) => void;
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
                  onClick={() => onShipmentClick(s.shipment_id)}
                  className="border-b border-gray-50 hover:bg-gray-50 transition-colors cursor-pointer"
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onShipmentClick(s.shipment_id);
                    }
                  }}
                  aria-label={`View details for shipment ${s.shipment_id}`}
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

// ─── Drill-Down Panel — Validates: Requirement 9.1 ───────────────────────────

interface ShipmentDrillDownPanelProps {
  shipmentId: string;
  onClose: () => void;
}

function ShipmentDrillDownPanel({
  shipmentId,
  onClose,
}: ShipmentDrillDownPanelProps) {
  const [detail, setDetail] = useState<ShipmentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function fetchDetail() {
      setLoading(true);
      setError("");
      try {
        const res = await getShipmentById(shipmentId);
        if (!cancelled) {
          setDetail(res.data);
        }
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Failed to load shipment detail",
          );
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    fetchDetail();
    return () => {
      cancelled = true;
    };
  }, [shipmentId]);

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/30"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Panel */}
      <div
        className="relative w-full max-w-lg bg-white shadow-xl flex flex-col overflow-hidden"
        role="dialog"
        aria-modal="true"
        aria-label={`Shipment detail for ${shipmentId}`}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 flex-shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            <Package className="w-5 h-5 text-[#232323] flex-shrink-0" />
            <h2 className="text-lg font-semibold text-[#232323] truncate">
              Shipment Detail
            </h2>
          </div>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close detail panel"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {loading && (
            <div className="flex items-center justify-center py-12">
              <LoadingSpinner message="Loading shipment detail..." />
            </div>
          )}

          {error && (
            <div className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
              {error}
            </div>
          )}

          {!loading && !error && detail && (
            <div className="space-y-6">
              {/* Shipment Info */}
              <section>
                <h3 className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-3">
                  Shipment Information
                </h3>
                <div className="space-y-3">
                  <DetailRow label="Shipment ID" value={detail.shipment_id} mono />
                  <DetailRow
                    label="Status"
                    value={
                      <span className="inline-block px-2 py-0.5 text-xs rounded bg-red-50 text-red-700 font-medium">
                        {detail.status}
                      </span>
                    }
                  />
                  <DetailRow
                    label="Failure Reason"
                    value={detail.failure_reason ?? "Unknown"}
                  />
                  <DetailRow
                    label="Rider"
                    icon={<User className="w-3.5 h-3.5" />}
                    value={detail.rider_id ?? "—"}
                  />
                  <DetailRow
                    label="Origin"
                    icon={<MapPin className="w-3.5 h-3.5" />}
                    value={detail.origin ?? "—"}
                  />
                  <DetailRow
                    label="Destination"
                    icon={<MapPin className="w-3.5 h-3.5" />}
                    value={detail.destination ?? "—"}
                  />
                  <DetailRow
                    label="Created"
                    icon={<Clock className="w-3.5 h-3.5" />}
                    value={
                      detail.created_at
                        ? new Date(detail.created_at).toLocaleString()
                        : "—"
                    }
                  />
                  <DetailRow
                    label="Last Updated"
                    icon={<Clock className="w-3.5 h-3.5" />}
                    value={
                      detail.updated_at
                        ? new Date(detail.updated_at).toLocaleString()
                        : "—"
                    }
                  />
                  {detail.estimated_delivery && (
                    <DetailRow
                      label="Estimated Delivery"
                      icon={<Clock className="w-3.5 h-3.5" />}
                      value={new Date(detail.estimated_delivery).toLocaleString()}
                    />
                  )}
                  {detail.trace_id && (
                    <DetailRow label="Trace ID" value={detail.trace_id} mono />
                  )}
                </div>
              </section>

              {/* Event Timeline */}
              <section>
                <h3 className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-3">
                  Event Timeline
                </h3>
                {detail.events && detail.events.length > 0 ? (
                  <div className="relative">
                    {/* Timeline line */}
                    <div className="absolute left-3 top-2 bottom-2 w-px bg-gray-200" />

                    <div className="space-y-4">
                      {detail.events.map((event, idx) => (
                        <div key={event.event_id} className="relative flex gap-3 pl-1">
                          {/* Timeline dot */}
                          <div
                            className={`relative z-10 w-5 h-5 rounded-full border-2 flex items-center justify-center flex-shrink-0 mt-0.5 ${
                              idx === 0
                                ? "border-[#232323] bg-[#232323]"
                                : "border-gray-300 bg-white"
                            }`}
                          >
                            {idx === 0 && (
                              <div className="w-1.5 h-1.5 rounded-full bg-white" />
                            )}
                          </div>

                          {/* Event content */}
                          <div className="flex-1 min-w-0 pb-1">
                            <div className="flex items-center gap-2 mb-0.5">
                              <span className="text-sm font-medium text-[#232323]">
                                {event.event_type}
                              </span>
                            </div>
                            <p className="text-xs text-gray-500">
                              {new Date(event.event_timestamp).toLocaleString()}
                            </p>
                            {event.location && (
                              <p className="text-xs text-gray-400 mt-0.5">
                                📍 {event.location.lat.toFixed(4)}, {event.location.lon.toFixed(4)}
                              </p>
                            )}
                            {event.event_payload &&
                              Object.keys(event.event_payload).length > 0 && (
                                <pre className="mt-1 text-xs text-gray-500 bg-gray-50 rounded px-2 py-1 overflow-x-auto">
                                  {JSON.stringify(event.event_payload, null, 2)}
                                </pre>
                              )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-gray-400">No events recorded</p>
                )}
              </section>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Helper component for rendering a labeled detail row */
function DetailRow({
  label,
  value,
  icon,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  icon?: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-start gap-2">
      <span className="text-xs text-gray-500 w-32 flex-shrink-0 pt-0.5 flex items-center gap-1">
        {icon}
        {label}
      </span>
      <span
        className={`text-sm text-[#232323] ${mono ? "font-mono text-xs" : ""}`}
      >
        {value}
      </span>
    </div>
  );
}
