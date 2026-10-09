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

import Link from "next/link";
import { useState } from "react";
import {
  type Column,
  DataTable,
  FilterChips,
  StatusBadge,
  type TableSort,
  Toolbar,
} from "@/components/ui";
import { calendarDate, dateTime, number, pct } from "../../lib/format";
import type { StatusKey } from "../../styles/tokens";
import { STATUS } from "../../styles/tokens";

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
  /**
   * Collapsed compliance qualification signal sourced from the correlated
   * driver profile read (GET /api/ops/drivers/{driver_id}/profile). Drives the
   * qualification-status chip. `null`/absent when no compliance record
   * correlates by driver_id (rendered as an "unlinked" affordance).
   */
  qualification_status?: "valid" | "expiring" | "expired" | null;
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

/** Driver duty status → badge style and label (icon + text). */
export const DUTY_STATUS: Record<
  DriverStatus,
  { status: StatusKey; label: string }
> = {
  active: { status: "ok", label: "Active" },
  on_break: { status: "warning", label: "On break" },
  off_duty: { status: "draft", label: "Off duty" },
  inactive: { status: "cancelled", label: "Inactive" },
};

/** Canonical asset destination (Fleet → Trucks, row selected). */
export const fleetAssetHref = (id: string) =>
  `/dashboard/fleet?tab=trucks&asset=${encodeURIComponent(id)}`;

const QUALIFICATION_CHIP: Record<
  NonNullable<DriverUtilization["qualification_status"]>,
  { status: StatusKey; label: string }
> = {
  expired: { status: "critical", label: "Expired" },
  expiring: { status: "warning", label: "Expiring" },
  valid: { status: "ok", label: "Valid" },
};

// ─── Component ───────────────────────────────────────────────────────────────

/**
 * Fleet → Drivers → Utilization table (UI revamp task 3.1): DataTable with
 * header sorting (`aria-sort` on `th`), status chips with counts in one
 * toolbar, StatusBadge for duty and qualification, a utilization bar with
 * the percentage as text, and an "Overloaded" badge instead of row tinting.
 */
export default function DriverUtilizationList({
  drivers,
  capacity = DEFAULT_CAPACITY,
  statusFilter = "",
  onStatusFilterChange,
}: DriverUtilizationListProps) {
  const [sort, setSort] = useState<TableSort>({
    key: "utilization",
    direction: "desc",
  });

  const filtered = statusFilter
    ? drivers.filter((d) => d.status === statusFilter)
    : drivers;

  const sorted = [...filtered].sort((a, b) => {
    const field = sort.key as SortField;
    let cmp = 0;
    switch (field) {
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
        cmp = String(a[field] ?? "").localeCompare(String(b[field] ?? ""));
    }
    return sort.direction === "asc" ? cmp : -cmp;
  });

  const counts: Record<string, number> = { "": drivers.length };
  for (const d of drivers) counts[d.status] = (counts[d.status] ?? 0) + 1;

  const columns: Column<DriverUtilization>[] = [
    {
      key: "driver_id",
      header: "Driver ID",
      sortable: true,
      className: "font-medium text-text",
      cell: (d) => d.driver_id,
    },
    {
      key: "driver_name",
      header: "Name",
      sortable: true,
      truncate: true,
      title: (d) => d.driver_name ?? undefined,
      className: "text-slate-700",
      cell: (d) => d.driver_name ?? "—",
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      width: 110,
      cell: (d) => {
        const cfg = DUTY_STATUS[d.status] ?? DUTY_STATUS.inactive;
        return <StatusBadge status={cfg.status} label={cfg.label} />;
      },
    },
    {
      key: "active_order_count",
      header: "Active Orders",
      sortable: true,
      align: "right",
      className: "tabular-nums text-slate-700",
      cell: (d) => (
        <span className="inline-flex items-center gap-1.5">
          {isOverloaded(d, capacity) && (
            <StatusBadge status="critical" label="Overloaded" />
          )}
          {number(d.active_order_count)}
        </span>
      ),
    },
    {
      key: "completed_today",
      header: "Completed Today",
      sortable: true,
      align: "right",
      className: "tabular-nums text-slate-700",
      cell: (d) => number(d.completed_today),
    },
    {
      key: "last_seen",
      header: "Last seen",
      sortable: true,
      className: "whitespace-nowrap text-slate-700",
      cell: (d) => (d.last_seen ? dateTime(d.last_seen) : "—"),
    },
    {
      key: "utilization",
      header: "Utilization",
      sortable: true,
      width: 160,
      cell: (d) => {
        const utilPct = getUtilization(d, capacity);
        const status: StatusKey =
          utilPct > 100 ? "critical" : utilPct >= 60 ? "warning" : "ok";
        return (
          <div className="flex items-center gap-2">
            <div
              className="h-2 flex-1 overflow-hidden rounded-full bg-slate-200"
              role="progressbar"
              aria-valuenow={utilPct}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={`Utilization ${utilPct}%`}
            >
              <div
                className="h-full rounded-full"
                style={{
                  width: `${Math.min(utilPct, 100)}%`,
                  backgroundColor: STATUS[status].dot,
                }}
              />
            </div>
            <span className="w-10 text-right text-xs tabular-nums text-slate-700">
              {pct(utilPct)}
            </span>
          </div>
        );
      },
    },
    {
      key: "medical_card",
      header: "Medical Card",
      cell: (d) => {
        const warning = getMedicalCardWarning(d);
        if (warning)
          return (
            <StatusBadge
              status={isMedicalCardExpired(d) ? "critical" : "warning"}
              label={warning}
            />
          );
        return (
          <span className="text-xs text-slate-700">
            {d.medical_card_expiry ? calendarDate(d.medical_card_expiry) : "—"}
          </span>
        );
      },
    },
    {
      key: "assigned_truck",
      header: "Truck",
      cell: (d) =>
        d.assigned_truck_id ? (
          <Link
            href={fleetAssetHref(d.assigned_truck_id)}
            className="text-sm font-medium text-link hover:underline"
            title={`View ${d.assigned_truck_id} in Fleet`}
          >
            {d.assigned_truck_id}
          </Link>
        ) : (
          <span className="text-xs text-text-muted">—</span>
        ),
    },
    {
      key: "qualification",
      header: "Qualification",
      cell: (d) => {
        const qs = d.qualification_status;
        if (!qs)
          return (
            <span
              className="text-xs text-text-muted"
              title="No qualification record"
            >
              Unlinked
            </span>
          );
        const chip = QUALIFICATION_CHIP[qs];
        return <StatusBadge status={chip.status} label={chip.label} />;
      },
    },
  ];

  return (
    <div className="flex h-full flex-col">
      {onStatusFilterChange && (
        <Toolbar
          label="Driver utilization"
          filters={
            <FilterChips
              label="Duty status"
              options={STATUS_OPTIONS.map((o) => ({
                id: o.value || "all",
                label: o.value ? DUTY_STATUS[o.value].label : "All",
                count: counts[o.value] ?? 0,
                status: o.value ? DUTY_STATUS[o.value].status : undefined,
              }))}
              value={statusFilter || "all"}
              onChange={(v) =>
                onStatusFilterChange(v === "all" ? "" : (v as DriverStatus))
              }
            />
          }
        />
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<DriverUtilization>
          ariaLabel="Driver utilization"
          columns={columns}
          data={sorted}
          getRowId={(d) => d.driver_id}
          rowLabel={(d) => d.driver_name ?? d.driver_id}
          sort={sort}
          onSortChange={setSort}
          emptyState={
            <div className="text-text-muted">
              <p className="text-sm font-medium">No drivers found</p>
              <p className="mt-1 text-xs">Try adjusting your filters</p>
            </div>
          }
        />
      </div>
    </div>
  );
}
