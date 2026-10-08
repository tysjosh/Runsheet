"use client";

import { MapPin, Pencil } from "lucide-react";
import { useState } from "react";
import {
  type Column,
  DataTable,
  ProductChip,
  StatusBadge,
  type TableSort,
} from "@/components/ui";
import { gallons, number, pct } from "../../lib/format";
import type { FuelStation, StationStatus } from "../../services/fuelApi";
import {
  getFuelStationCapacityGallons,
  getFuelStationCurrentStockGallons,
} from "../../services/fuelApi";
import type { StatusKey } from "../../styles/tokens";
import { STATUS } from "../../styles/tokens";

interface FuelStationListProps {
  stations: FuelStation[];
  /** Called when a station row is clicked */
  onSelectStation?: (stationId: string) => void;
  /** Currently selected station ID */
  selectedStationId?: string | null;
  /** Called when the Edit action is chosen for a station */
  onEditStation?: (station: FuelStation) => void;
  /** Skeleton rows while the first load runs. */
  loading?: boolean;
}

/** Station stock status → display status (hue + icon) and its label. */
export const STATION_STATUS: Record<
  StationStatus,
  { status: StatusKey; label: string }
> = {
  normal: { status: "ok", label: "Normal" },
  low: { status: "warning", label: "Low" },
  critical: { status: "critical", label: "Critical" },
  empty: { status: "exception", label: "Empty" },
};

export function getStockPercentage(station: FuelStation): number {
  const capacity = getFuelStationCapacityGallons(station);
  if (capacity <= 0) return 0;
  return (getFuelStationCurrentStockGallons(station) / capacity) * 100;
}

type SortKey =
  | "name"
  | "fuel_type"
  | "status"
  | "stock_pct"
  | "days_until_empty"
  | "location_name";

/**
 * Station list (DataTable): product chip, stock bar coloured by status with
 * the percentage and gallons as text, a status badge, days left and
 * location. Sorting is on the column headers (`aria-sort` on `th`).
 *
 * Validates: Requirements 6.1, 6.4
 */
export default function FuelStationList({
  stations,
  onSelectStation,
  selectedStationId,
  onEditStation,
  loading = false,
}: FuelStationListProps) {
  const [sort, setSort] = useState<TableSort>({
    key: "stock_pct",
    direction: "asc",
  });

  const sorted = [...stations].sort((a, b) => {
    const key = sort.key as SortKey;
    let cmp = 0;
    if (key === "stock_pct")
      cmp = getStockPercentage(a) - getStockPercentage(b);
    else if (key === "days_until_empty")
      cmp = (a.days_until_empty ?? 0) - (b.days_until_empty ?? 0);
    else cmp = String(a[key] ?? "").localeCompare(String(b[key] ?? ""));
    return sort.direction === "asc" ? cmp : -cmp;
  });

  const columns: Column<FuelStation>[] = [
    {
      key: "name",
      header: "Station",
      sortable: true,
      truncate: true,
      title: (s) => s.name,
      className: "font-medium text-text",
      cell: (s) => s.name,
    },
    {
      key: "fuel_type",
      header: "Product",
      sortable: true,
      width: 200,
      cell: (s) => <ProductChip code={s.fuel_type} variant="chip" />,
    },
    {
      key: "stock_pct",
      header: "Stock",
      sortable: true,
      width: 240,
      cell: (s) => {
        const p = getStockPercentage(s);
        const cfg = STATION_STATUS[s.status] ?? STATION_STATUS.normal;
        return (
          <div className="flex items-center gap-2">
            <div
              className="h-2 w-20 shrink-0 overflow-hidden rounded-full bg-slate-200"
              role="progressbar"
              aria-valuenow={Math.round(p)}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={`Stock level ${pct(p)}`}
            >
              <div
                className="h-full rounded-full"
                style={{
                  width: `${Math.min(p, 100)}%`,
                  backgroundColor: STATUS[cfg.status].dot,
                }}
              />
            </div>
            <span className="whitespace-nowrap text-xs tabular-nums text-slate-700">
              {pct(p)} · {number(getFuelStationCurrentStockGallons(s))} /{" "}
              {gallons(getFuelStationCapacityGallons(s))}
            </span>
          </div>
        );
      },
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      width: 110,
      cell: (s) => {
        const cfg = STATION_STATUS[s.status] ?? STATION_STATUS.normal;
        return <StatusBadge status={cfg.status} label={cfg.label} />;
      },
    },
    {
      key: "days_until_empty",
      header: "Days left",
      sortable: true,
      align: "right",
      width: 100,
      className: "tabular-nums text-slate-700",
      cell: (s) =>
        s.days_until_empty > 0
          ? `${number(s.days_until_empty, { decimals: 1 })} d`
          : "—",
    },
    {
      key: "location_name",
      header: "Location",
      sortable: true,
      truncate: true,
      title: (s) => s.location_name ?? undefined,
      className: "text-slate-700",
      cell: (s) =>
        s.location_name ? (
          <span className="inline-flex items-center gap-1">
            <MapPin className="h-3 w-3 text-slate-500" aria-hidden="true" />
            {s.location_name}
          </span>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <DataTable<FuelStation>
      ariaLabel="Fuel station list"
      columns={columns}
      data={sorted}
      loading={loading}
      getRowId={(s) => s.station_id}
      selectedId={selectedStationId ?? undefined}
      sort={sort}
      onSortChange={setSort}
      rowLabel={(s) => s.name}
      rowMenu={
        onEditStation
          ? (s) => [
              {
                id: "edit",
                label: "Edit station",
                icon: <Pencil className="h-3.5 w-3.5" />,
                onSelect: () => onEditStation(s),
              },
            ]
          : undefined
      }
      onRowClick={
        onSelectStation ? (s) => onSelectStation(s.station_id) : undefined
      }
      emptyState={
        <div className="text-text-muted">
          <p className="text-sm font-medium">No stations found</p>
          <p className="mt-1 text-xs">Try adjusting your filters</p>
        </div>
      }
    />
  );
}
