"use client";

/**
 * Driver Utilization List — renamed from RiderUtilizationList.
 *
 * Displays driver utilization with sortable columns, utilization bars,
 * and color-coded highlighting. Updated from rider fields to driver
 * fields (active_order_count, completed_today, medical_card_expiry warning).
 *
 * Validates: Requirements 3.1.4, 8.1.2
 */

import { AlertTriangle, ChevronDown, ChevronUp } from "lucide-react";
import { useCallback, useState } from "react";

// ─── Types ───────────────────────────────────────────────────────────────────

export type DriverStatus = "active" | "on_break" | "off_duty" | "inactive";

export interface DriverUtilization {
  driver_id: string;
  driver_name?: string | null;
  status: DriverStatus;
  active_order_count: number;
  completed_today: number;
  last_seen?: string | null;
  medical_card_expiry?: string | null;
  assigned_truck_id?: string | null;
  cdl_class?: string | null;
  hazmat_endorsement?: boolean | null;
  utilization_percentage?: number | null;
}

// ─── Constants ───────────────────────────────────────────────────────────────

const DEFAULT_CAPACITY = 8;
const MEDICAL_CARD_WARNING_DAYS = 30;

type SortField =
  | "driver_id"
  | "driver_name"
  | "status"
  | "active_order_count"
  | "completed_today"
  | "last_seen"
  | "utilization";
type SortOrder = "asc" | "desc";

interface DriverUtilizationListProps {
  drivers: DriverUtilization[];
  /** Capacity threshold for utilization bar. Defaults to 8. */
  capacity?: number;
  /** Filter drivers by status */
  statusFilter?: DriverStatus | "";
  /** Callback when status filter changes */
  onStatusFilterChange?: (status: DriverStatus | "") => void;
}

const STATUS_OPTIONS: { value: DriverStatus | ""; label: string }[] = [
  { value: "", label: "All Statuses" },
  { value: "active", label: "Active" },
  { value: "on_break", label: "On Break" },
  { value: "off_duty", label: "Off Duty" },
  { value: "inactive", label: "Inactive" },
];

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getUtilization(driver: DriverUtilization, capacity: number): number {
  if (driver.utilization_percentage != null)
    return driver.utilization_percentage;
  if (capacity <= 0) return 0;
  return Math.round((driver.active_order_count / capacity) * 100);
}

function isOverloaded(driver: DriverUtilization, capacity: number): boolean {
  return driver.active_order_count > capacity;
}

function getRowHighlight(driver: DriverUtilization, capacity: number): string {
  if (isOverloaded(driver, capacity)) return "bg-error-light";
  if (isMedicalCardExpiring(driver)) return "bg-warning-light";
  return "";
}

function isMedicalCardExpiring(driver: DriverUtilization): boolean {
  if (!driver.medical_card_expiry) return false;
  const expiry = new Date(driver.medical_card_expiry);
  const now = new Date();
  const daysUntilExpiry = Math.floor(
    (expiry.getTime() - now.getTime()) / (1000 * 60 * 60 * 24),
  );
  return daysUntilExpiry <= MEDICAL_CARD_WARNING_DAYS;
}

function isMedicalCardExpired(driver: DriverUtilization): boolean {
  if (!driver.medical_card_expiry) return false;
  return new Date(driver.medical_card_expiry) < new Date();
}

function getMedicalCardWarning(driver: DriverUtilization): string | null {
  if (isMedicalCardExpired(driver)) return "Expired";
  if (isMedicalCardExpiring(driver)) return "Expiring soon";
  return null;
}

function getStatusBadge(status: DriverStatus): string {
  switch (status) {
    case "active":
      return "text-success-dark bg-success-light";
    case "on_break":
      return "text-warning-dark bg-warning-light";
    case "off_duty":
      return "text-warning-dark bg-warning-light";
    case "inactive":
      return "text-gray-700 bg-gray-100";
    default:
      return "text-gray-700 bg-gray-100";
  }
}

function getBarColor(percentage: number): string {
  if (percentage > 100) return "bg-error";
  if (percentage >= 60) return "bg-warning";
  return "bg-success";
}

function formatDate(dateStr?: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function DriverUtilizationList({
  drivers,
  capacity = DEFAULT_CAPACITY,
  statusFilter = "",
  onStatusFilterChange,
}: DriverUtilizationListProps) {
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
    ? drivers.filter((d) => d.status === statusFilter)
    : drivers;

  // Sort
  const sorted = [...filtered].sort((a, b) => {
    let cmp = 0;
    switch (sortField) {
      case "utilization":
        cmp = getUtilization(a, capacity) - getUtilization(b, capacity);
        break;
      case "active_order_count":
        cmp = a.active_order_count - b.active_order_count;
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
    { key: "driver_id", label: "Driver ID" },
    { key: "driver_name", label: "Name" },
    { key: "status", label: "Status" },
    { key: "active_order_count", label: "Active Orders" },
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
      {/* Header */}
      <div className="px-6 py-4 border-b border-gray-100">
        <h2 className="text-lg font-semibold text-primary">
          Driver Utilization
        </h2>
      </div>

      {/* Status filter */}
      {onStatusFilterChange && (
        <div className="px-6 py-3 border-b border-gray-100">
          <label htmlFor="driver-status-filter" className="sr-only">
            Filter by status
          </label>
          <select
            id="driver-status-filter"
            value={statusFilter}
            onChange={(e) =>
              onStatusFilterChange(e.target.value as DriverStatus | "")
            }
            className="rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
            aria-label="Filter drivers by status"
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
          <p className="text-lg font-medium text-gray-400">No drivers found</p>
          <p className="text-sm text-gray-400 mt-1">
            Try adjusting your filters
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full" aria-label="Driver utilization list">
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
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Medical Card
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {sorted.map((driver) => {
                const utilPct = getUtilization(driver, capacity);
                const barWidth = Math.min(utilPct, 100);
                const medWarning = getMedicalCardWarning(driver);

                return (
                  <tr
                    key={driver.driver_id}
                    className={`${getRowHighlight(driver, capacity)} transition-colors`}
                  >
                    <td className="px-6 py-3 text-sm font-medium text-primary">
                      {driver.driver_id}
                    </td>
                    <td className="px-6 py-3 text-sm text-gray-700">
                      {driver.driver_name ?? "—"}
                    </td>
                    <td className="px-6 py-3">
                      <span
                        className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(driver.status)}`}
                      >
                        {driver.status.replace("_", " ")}
                      </span>
                    </td>
                    <td className="px-6 py-3 text-sm text-gray-700">
                      {driver.active_order_count}
                    </td>
                    <td className="px-6 py-3 text-sm text-gray-700">
                      {driver.completed_today}
                    </td>
                    <td className="px-6 py-3 text-sm text-gray-600">
                      {formatDate(driver.last_seen)}
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
                    <td className="px-6 py-3">
                      {medWarning ? (
                        <span
                          className={`inline-flex items-center gap-1 text-xs font-medium ${isMedicalCardExpired(driver) ? "text-error-dark" : "text-warning-dark"}`}
                        >
                          <AlertTriangle className="w-3 h-3" />
                          {medWarning}
                        </span>
                      ) : driver.medical_card_expiry ? (
                        <span className="text-xs text-gray-500">
                          {formatDate(driver.medical_card_expiry)}
                        </span>
                      ) : (
                        <span className="text-xs text-gray-400">—</span>
                      )}
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
