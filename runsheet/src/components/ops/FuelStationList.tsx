"use client";

import { ChevronDown, ChevronUp, MapPin, Pencil } from "lucide-react";
import { useCallback, useState } from "react";
import type { FuelStation, FuelType, StationStatus } from "../../services/fuelApi";

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

const STATUS_CONFIG: Record<StationStatus, { label: string; color: string; bg: string; barColor: string }> = {
  normal:   { label: "Normal",   color: "text-green-700",  bg: "bg-green-100",  barColor: "bg-green-500" },
  low:      { label: "Low",      color: "text-yellow-700", bg: "bg-yellow-100", barColor: "bg-yellow-500" },
  critical: { label: "Critical", color: "text-red-700",    bg: "bg-red-100",    barColor: "bg-red-500" },
  empty:    { label: "Empty",    color: "text-gray-700",   bg: "bg-gray-100",   barColor: "bg-gray-400" },
};

const FUEL_TYPE_LABELS: Record<FuelType, string> = {
  AGO: "AGO (Diesel)",
  PMS: "PMS (Petrol)",
  ATK: "ATK (Aviation)",
  LPG: "LPG (Gas)",
};

function getStockPercentage(station: FuelStation): number {
  if (station.capacity_liters <= 0) return 0;
  return (station.current_stock_liters / station.capacity_liters) * 100;
}

function formatLiters(liters: number): string {
  if (liters >= 1_000) return `${(liters / 1_000).toFixed(1)}K`;
  return liters.toFixed(0);
}

const COLUMNS: { key: SortField; label: string }[] = [
  { key: "name", label: "Station" },
  { key: "fuel_type", label: "Fuel Type" },
  { key: "stock_pct", label: "Stock Level" },
  { key: "status", label: "Status" },
  { key: "days_until_empty", label: "Days Left" },
  { key: "location_name", label: "Location" },
];

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
        cmp = (String(a[sortField] ?? "")).localeCompare(String(b[sortField] ?? ""));
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

  if (stations.length === 0) {
    return (
      <div className="text-center py-16 text-gray-500">
        <p className="text-lg font-medium text-gray-400">No stations found</p>
        <p className="text-sm text-gray-400 mt-1">Try adjusting your filters</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full" aria-label="Fuel station list">
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
            {onEditStation && (
              <th className="px-6 py-3 text-right text-xs font-medium text-gray-600 uppercase tracking-wider">
                Actions
              </th>
            )}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {sorted.map((station) => {
            const stockPct = getStockPercentage(station);
            const config = STATUS_CONFIG[station.status] ?? STATUS_CONFIG.normal;
            const isSelected = selectedStationId === station.station_id;

            return (
              <tr
                key={`${station.station_id}::${station.fuel_type}`}
                className={`transition-colors cursor-pointer ${
                  isSelected ? "bg-blue-50" : "hover:bg-gray-50"
                }`}
                onClick={() => onSelectStation?.(station.station_id)}
                role="button"
                tabIndex={0}
                aria-selected={isSelected}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelectStation?.(station.station_id);
                  }
                }}
              >
                <td className="px-6 py-3 text-sm font-medium text-[#232323]">
                  {station.name}
                </td>
                <td className="px-6 py-3 text-sm text-gray-700">
                  {FUEL_TYPE_LABELS[station.fuel_type] ?? station.fuel_type}
                </td>
                <td className="px-6 py-3">
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
                    {formatLiters(station.current_stock_liters)} / {formatLiters(station.capacity_liters)} L
                  </div>
                </td>
                <td className="px-6 py-3">
                  <span
                    className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${config.bg} ${config.color}`}
                  >
                    {config.label}
                  </span>
                </td>
                <td className="px-6 py-3 text-sm text-gray-700">
                  {station.days_until_empty > 0
                    ? `${station.days_until_empty.toFixed(1)} days`
                    : "—"}
                </td>
                <td className="px-6 py-3 text-sm text-gray-600">
                  {station.location_name ? (
                    <span className="flex items-center gap-1">
                      <MapPin className="w-3 h-3 text-gray-400" aria-hidden="true" />
                      {station.location_name}
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
                {onEditStation && (
                  <td className="px-6 py-3 text-right">
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
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
