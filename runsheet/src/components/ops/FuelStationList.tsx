"use client";

import { ChevronDown, ChevronUp, MapPin, Pencil } from "lucide-react";
import { useCallback, useState } from "react";
import { type Column, Table } from "@/components/ui";
import type {
  FuelStation,
  FuelType,
  StationStatus,
} from "../../services/fuelApi";
import {
  getFuelStationCapacityGallons,
  getFuelStationCurrentStockGallons,
} from "../../services/fuelApi";

type SortField =
  | "name"
  | "fuel_type"
  | "status"
  | "stock_pct"
  | "days_until_empty"
  | "location_name";
type SortOrder = "asc" | "desc";

interface FuelStationListProps {
  stations: FuelStation[];
  /** Called when a station row is clicked */
  onSelectStation?: (stationId: string) => void;
  /** Currently selected station ID */
  selectedStationId?: string | null;
  /** Called when the Edit button is clicked for a station */
  onEditStation?: (station: FuelStation) => void;
}

const STATUS_CONFIG: Record<
  StationStatus,
  { label: string; color: string; bg: string; barColor: string }
> = {
  normal: {
    label: "Normal",
    color: "text-success-dark",
    bg: "bg-success-light",
    barColor: "bg-success",
  },
  low: {
    label: "Low",
    color: "text-warning-dark",
    bg: "bg-warning-light",
    barColor: "bg-warning",
  },
  critical: {
    label: "Critical",
    color: "text-error-dark",
    bg: "bg-error-light",
    barColor: "bg-error",
  },
  empty: {
    label: "Empty",
    color: "text-gray-700",
    bg: "bg-gray-100",
    barColor: "bg-gray-400",
  },
};

const FUEL_TYPE_LABELS: Record<FuelType, string> = {
  DIESEL_2: "Diesel #2 (ULSD)",
  GASOLINE_REG: "Regular Unleaded",
  GASOLINE_PREM: "Premium Unleaded",
  HEATING_OIL: "Heating Oil",
  PROPANE: "Propane",
  KEROSENE: "Kerosene",
  OFF_ROAD_DIESEL: "Off-Road Diesel",
  DEF: "DEF",
};

function getCapacityGallons(station: FuelStation): number {
  return getFuelStationCapacityGallons(station);
}

function getCurrentStockGallons(station: FuelStation): number {
  return getFuelStationCurrentStockGallons(station);
}

function getStockPercentage(station: FuelStation): number {
  const capacityGallons = getCapacityGallons(station);
  if (capacityGallons <= 0) return 0;
  return (getCurrentStockGallons(station) / capacityGallons) * 100;
}

function formatGallons(gallons: number): string {
  if (gallons == null || Number.isNaN(gallons)) return "0";
  if (gallons >= 1_000) return `${(gallons / 1_000).toFixed(1)}K`;
  return gallons.toFixed(0);
}

/**
 * Station list with stock percentage bars, status color-coding
 * (green/yellow/red/gray), fuel type, and location.
 *
 * Validates: Requirements 6.1, 6.4
 */
export default function FuelStationList({
  stations,
  onSelectStation,
  selectedStationId,
  onEditStation,
}: FuelStationListProps) {
  const [sortField, setSortField] = useState<SortField>("stock_pct");
  const [sortOrder, setSortOrder] = useState<SortOrder>("asc");

  const handleSort = useCallback(
    (field: SortField) => {
      if (sortField === field) {
        setSortOrder((prev) => (prev === "asc" ? "desc" : "asc"));
      } else {
        setSortField(field);
        setSortOrder(field === "stock_pct" ? "asc" : "desc");
      }
    },
    [sortField],
  );

  const sorted = [...stations].sort((a, b) => {
    let cmp = 0;
    switch (sortField) {
      case "stock_pct":
        cmp = getStockPercentage(a) - getStockPercentage(b);
        break;
      case "days_until_empty":
        cmp = a.days_until_empty - b.days_until_empty;
        break;
      default:
        cmp = String(a[sortField] ?? "").localeCompare(
          String(b[sortField] ?? ""),
        );
    }
    return sortOrder === "asc" ? cmp : -cmp;
  });

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return null;
    return sortOrder === "asc" ? (
      <ChevronUp className="w-3 h-3 inline ml-1" />
    ) : (
      <ChevronDown className="w-3 h-3 inline ml-1" />
    );
  };

  const SortableHeader = ({
    field,
    label,
  }: {
    field: SortField;
    label: string;
  }) => (
    <button
      type="button"
      onClick={() => handleSort(field)}
      aria-sort={
        sortField === field
          ? sortOrder === "asc"
            ? "ascending"
            : "descending"
          : "none"
      }
      className="flex items-center text-xs font-medium text-gray-600 uppercase tracking-wider"
    >
      {label}
      <SortIcon field={field} />
    </button>
  );

  const columns: Column<FuelStation>[] = [
    {
      key: "name",
      label: <SortableHeader field="name" label="Station" />,
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm font-medium text-primary",
      render: (station) => station.name,
    },
    {
      key: "fuel_type",
      label: <SortableHeader field="fuel_type" label="Fuel Type" />,
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm text-gray-700",
      render: (station) =>
        FUEL_TYPE_LABELS[station.fuel_type] ?? station.fuel_type,
    },
    {
      key: "stock_pct",
      label: <SortableHeader field="stock_pct" label="Stock Level" />,
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      render: (station) => {
        const stockPct = getStockPercentage(station);
        const config = STATUS_CONFIG[station.status] ?? STATUS_CONFIG.normal;
        return (
          <>
            <div className="flex items-center gap-2">
              <div
                className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden"
                role="progressbar"
                aria-valuenow={Math.round(stockPct)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`Stock level ${Math.round(stockPct)}%`}
              >
                <div
                  className={`h-full rounded-full transition-all ${config.barColor}`}
                  style={{ width: `${Math.min(stockPct, 100)}%` }}
                />
              </div>
              <span className="text-xs text-gray-600 w-16 text-right">
                {stockPct.toFixed(1)}%
              </span>
            </div>
            <div className="text-xs text-gray-400 mt-0.5">
              {formatGallons(getCurrentStockGallons(station))} /{" "}
              {formatGallons(getCapacityGallons(station))} gal
            </div>
          </>
        );
      },
    },
    {
      key: "status",
      label: <SortableHeader field="status" label="Status" />,
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      render: (station) => {
        const config = STATUS_CONFIG[station.status] ?? STATUS_CONFIG.normal;
        return (
          <span
            className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${config.bg} ${config.color}`}
          >
            {config.label}
          </span>
        );
      },
    },
    {
      key: "days_until_empty",
      label: <SortableHeader field="days_until_empty" label="Days Left" />,
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm text-gray-700",
      render: (station) =>
        station.days_until_empty > 0
          ? `${station.days_until_empty.toFixed(1)} days`
          : "—",
    },
    {
      key: "location_name",
      label: <SortableHeader field="location_name" label="Location" />,
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm text-gray-600",
      render: (station) =>
        station.location_name ? (
          <span className="flex items-center gap-1">
            <MapPin className="w-3 h-3 text-gray-400" aria-hidden="true" />
            {station.location_name}
          </span>
        ) : (
          "—"
        ),
    },
  ];

  if (onEditStation) {
    columns.push({
      key: "actions",
      label: "Actions",
      align: "right",
      render: (station) => (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onEditStation(station);
          }}
          className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-gray-600 bg-gray-100 rounded-md hover:bg-gray-200 hover:text-gray-800 transition-colors"
          aria-label={`Edit ${station.name}`}
        >
          <Pencil className="w-3 h-3" aria-hidden="true" />
          Edit
        </button>
      ),
    });
  }

  return (
    <Table<FuelStation>
      ariaLabel="Fuel station list"
      columns={columns}
      data={sorted}
      variant="compact"
      getRowId={(station) => station.station_id}
      selectedId={selectedStationId ?? undefined}
      onRowClick={
        onSelectStation
          ? (station) => onSelectStation(station.station_id)
          : undefined
      }
      emptyState={
        <div className="text-gray-500">
          <p className="text-lg font-medium text-gray-400">No stations found</p>
          <p className="text-sm text-gray-400 mt-1">
            Try adjusting your filters
          </p>
        </div>
      }
    />
  );
}
