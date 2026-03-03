"use client";

import { ChevronDown, ChevronUp } from "lucide-react";
import { useCallback, useState } from "react";
import type { RiderStatus, RiderUtilization } from "../../services/opsApi";

/** Default capacity threshold for utilization calculation */
const DEFAULT_CAPACITY = 10;

/** Idle threshold in minutes — riders idle longer than this are highlighted */
const IDLE_THRESHOLD_MINUTES = 30;

type SortField =
  | "rider_id"
  | "rider_name"
  | "status"
  | "active_shipment_count"
  | "completed_today"
  | "last_seen"
  | "utilization";
type SortOrder = "asc" | "desc";

interface RiderUtilizationListProps {
  riders: RiderUtilization[];
  /** Capacity threshold for utilization bar. Defaults to 10. */
  capacity?: number;
  /** Filter riders by status */
  statusFilter?: RiderStatus | "";
  /** Callback when status filter changes */
  onStatusFilterChange?: (status: RiderStatus | "") => void;
}

const STATUS_OPTIONS: { value: RiderStatus | ""; label: string }[] = [
  { value: "", label: "All Statuses" },
  { value: "active", label: "Active" },
  { value: "idle", label: "Idle" },
  { value: "offline", label: "Offline" },
];

/**
 * Returns utilization percentage for a rider.
 * Uses the API-provided utilization_percentage if available,
 * otherwise calculates from active_shipment_count / capacity.
 */
function getUtilization(rider: RiderUtilization, capacity: number): number {
  if (rider.utilization_percentage != null) return rider.utilization_percentage;
  if (capacity <= 0) return 0;
  return Math.round((rider.active_shipment_count / capacity) * 100);
}

/**
 * Returns idle duration in minutes since last_seen.
 */
function getIdleMinutes(rider: RiderUtilization): number {
  if (rider.idle_minutes != null) return rider.idle_minutes;
  if (!rider.last_seen) return 0;
  const diff = Date.now() - new Date(rider.last_seen).getTime();
  return Math.max(0, Math.floor(diff / 60000));
}

/**
 * Determines if a rider is overloaded (exceeds capacity).
 * Validates: Requirement 13.3 — highlight overloaded riders in red
 */
function isOverloaded(rider: RiderUtilization, capacity: number): boolean {
  return rider.active_shipment_count > capacity;
}

/**
 * Determines if a rider has been idle for more than 30 minutes.
 * Validates: Requirement 13.3 — highlight idle >30min riders in yellow
 */
function isIdleTooLong(rider: RiderUtilization): boolean {
  if (rider.status !== "idle") return false;
  return getIdleMinutes(rider) > IDLE_THRESHOLD_MINUTES;
}

/**
 * Returns the row highlight class based on rider state.
 * Validates: Requirement 13.3
 */
function getRowHighlight(rider: RiderUtilization, capacity: number): string {
  if (isOverloaded(rider, capacity)) return "bg-red-50";
  if (isIdleTooLong(rider)) return "bg-yellow-50";
  return "";
}

function getStatusBadge(status: RiderStatus): string {
  switch (status) {
    case "active":
      return "text-green-700 bg-green-100";
    case "idle":
      return "text-yellow-700 bg-yellow-100";
    case "offline":
      return "text-gray-700 bg-gray-100";
    default:
      return "text-gray-700 bg-gray-100";
  }
}

/**
 * Returns the utilization bar color based on percentage.
 * Red when overloaded (>100%), yellow when moderate (60-100%), green otherwise.
 */
function getBarColor(percentage: number): string {
  if (percentage > 100) return "bg-red-500";
  if (percentage >= 60) return "bg-yellow-500";
  return "bg-green-500";
}

function formatDate(dateStr?: string): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Rider utilization list with sortable columns, utilization bars,
 * and color-coded highlighting for overloaded/idle riders.
 *
 * Validates: Requirements 13.1-13.4
 */
export default function RiderUtilizationList({
  riders,
  capacity = DEFAULT_CAPACITY,
  statusFilter = "",
  onStatusFilterChange,
}: RiderUtilizationListProps) {
  const [sortField, setSortField] = useState<SortField>("utilization");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");

  const handleSort = useCallback(
    (field: SortField) => {
      if (sortField === field) {
        setSortOrder((prev) => (prev === "asc" ? "desc" : "asc"));
      } else {
        setSortField(field);
        setSortOrder("desc");
      }
    },
    [sortField],
  );

  // Filter by status
  const filtered = statusFilter
    ? riders.filter((r) => r.status === statusFilter)
    : riders;

  // Sort
  const sorted = [...filtered].sort((a, b) => {
    let cmp = 0;
    switch (sortField) {
      case "utilization":
        cmp = getUtilization(a, capacity) - getUtilization(b, capacity);
        break;
      case "active_shipment_count":
        cmp = a.active_shipment_count - b.active_shipment_count;
        break;
      case "completed_today":
        cmp = a.completed_today - b.completed_today;
        break;
      case "last_seen":
        cmp = (a.last_seen ?? "").localeCompare(b.last_seen ?? "");
        break;
      default:
        cmp = ((a[sortField] as string) ?? "").localeCompare(
          (b[sortField] as string) ?? "",
        );
    }
    return sortOrder === "asc" ? cmp : -cmp;
  });

  const COLUMNS: { key: SortField; label: string }[] = [
    { key: "rider_id", label: "Rider ID" },
    { key: "rider_name", label: "Name" },
    { key: "status", label: "Status" },
    { key: "active_shipment_count", label: "Shipments" },
    { key: "completed_today", label: "Completed Today" },
    { key: "last_seen", label: "Last Seen" },
    { key: "utilization", label: "Utilization" },
  ];

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return null;
    return sortOrder === "asc" ? (
      <ChevronUp className="w-3 h-3 inline ml-1" />
    ) : (
      <ChevronDown className="w-3 h-3 inline ml-1" />
    );
  };

  return (
    <div>
      {/* Status filter */}
      {onStatusFilterChange && (
        <div className="px-6 py-3 border-b border-gray-100">
          <label htmlFor="rider-status-filter" className="sr-only">
            Filter by status
          </label>
          <select
            id="rider-status-filter"
            value={statusFilter}
            onChange={(e) =>
              onStatusFilterChange(e.target.value as RiderStatus | "")
            }
            className="rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-[#232323] focus:border-transparent"
            aria-label="Filter riders by status"
          >
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      )}

      {filtered.length === 0 ? (
        <div className="text-center py-16 text-gray-500">
          <p className="text-lg font-medium text-gray-400">No riders found</p>
          <p className="text-sm text-gray-400 mt-1">
            Try adjusting your filters
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full" aria-label="Rider utilization list">
            <thead className="bg-gray-50 sticky top-0 border-b border-gray-100">
              <tr>
                {COLUMNS.map((col) => (
                  <th
                    key={col.key}
                    className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer select-none hover:bg-gray-100"
                    onClick={() => handleSort(col.key)}
                    aria-sort={
                      sortField === col.key
                        ? sortOrder === "asc"
                          ? "ascending"
                          : "descending"
                        : "none"
                    }
                  >
                    {col.label}
                    <SortIcon field={col.key} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {sorted.map((rider) => {
                const utilPct = getUtilization(rider, capacity);
                const barWidth = Math.min(utilPct, 100);

                return (
                  <tr
                    key={rider.rider_id}
                    className={`${getRowHighlight(rider, capacity)} transition-colors`}
                  >
                    <td className="px-6 py-3 text-sm font-medium text-[#232323]">
                      {rider.rider_id}
                    </td>
                    <td className="px-6 py-3 text-sm text-gray-700">
                      {rider.rider_name ?? "—"}
                    </td>
                    <td className="px-6 py-3">
                      <span
                        className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(rider.status)}`}
                      >
                        {rider.status}
                      </span>
                    </td>
                    <td className="px-6 py-3 text-sm text-gray-700">
                      {rider.active_shipment_count}
                    </td>
                    <td className="px-6 py-3 text-sm text-gray-700">
                      {rider.completed_today}
                    </td>
                    <td className="px-6 py-3 text-sm text-gray-600">
                      {formatDate(rider.last_seen)}
                    </td>
                    <td className="px-6 py-3">
                      <div className="flex items-center gap-2">
                        <div
                          className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden"
                          role="progressbar"
                          aria-valuenow={utilPct}
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-label={`Utilization ${utilPct}%`}
                        >
                          <div
                            className={`h-full rounded-full transition-all ${getBarColor(utilPct)}`}
                            style={{ width: `${barWidth}%` }}
                          />
                        </div>
                        <span className="text-xs text-gray-600 w-10 text-right">
                          {utilPct}%
                        </span>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
