"use client";

import {
  ArrowDown,
  ArrowUp,
  Clock,
  Droplets,
  Fuel,
  MapPin,
  TrendingDown,
  X,
} from "lucide-react";
import { useState } from "react";
import type {
  FuelStation,
  FuelStationDetail as FuelStationDetailType,
  FuelType,
  StationStatus,
} from "../../services/fuelApi";
import {
  getEventQuantityGallons,
  getFuelStationCapacityGallons,
  getFuelStationCurrentStockGallons,
  getFuelStationDailyConsumptionGallons,
} from "../../services/fuelApi";
import FuelEventForm from "./FuelEventForm";

interface FuelStationDetailProps {
  detail: FuelStationDetailType;
  onClose?: () => void;
  onEventRecorded?: () => void;
}

const STATUS_CONFIG: Record<
  StationStatus,
  { label: string; color: string; bg: string }
> = {
  normal: {
    label: "Normal",
    color: "text-success-dark",
    bg: "bg-success-light",
  },
  low: { label: "Low", color: "text-warning-dark", bg: "bg-warning-light" },
  critical: {
    label: "Critical",
    color: "text-error-dark",
    bg: "bg-error-light",
  },
  empty: { label: "Empty", color: "text-gray-700", bg: "bg-gray-100" },
};

const STATUS_BAR_COLORS: Record<StationStatus, string> = {
  normal: "bg-success-light0",
  low: "bg-warning-light0",
  critical: "bg-error-light0",
  empty: "bg-gray-400",
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

function formatGallons(gallons: number): string {
  if (gallons >= 1_000_000) return `${(gallons / 1_000_000).toFixed(1)}M gal`;
  if (gallons >= 1_000) return `${(gallons / 1_000).toFixed(1)}K gal`;
  return `${gallons.toFixed(0)} gal`;
}

/**
 * Station detail panel showing stock level, recent consumption events,
 * recent refill events, and daily consumption rate.
 *
 * Validates: Requirements 6.6
 */
export default function FuelStationDetail({
  detail,
  onClose,
  onEventRecorded,
}: FuelStationDetailProps) {
  const { station, recent_consumption_events, recent_refill_events } = detail;
  const [activeForm, setActiveForm] = useState<"consumption" | "refill" | null>(
    null,
  );
  const stockPct =
    getCapacityGallons(station) > 0
      ? (getCurrentStockGallons(station) / getCapacityGallons(station)) * 100
      : 0;
  const statusCfg = STATUS_CONFIG[station.status] ?? STATUS_CONFIG.normal;
  const barColor = STATUS_BAR_COLORS[station.status] ?? "bg-gray-400";

  return (
    <div
      className="bg-white border border-gray-200 rounded-lg shadow-sm"
      role="region"
      aria-label={`Station detail: ${station.name}`}
    >
      {/* Header */}
      <div className="flex items-start justify-between p-4 border-b border-gray-100">
        <div>
          <h3 className="text-lg font-semibold text-primary">{station.name}</h3>
          <div className="flex items-center gap-3 mt-1 text-sm text-gray-500">
            <span className="flex items-center gap-1">
              <Fuel className="w-3.5 h-3.5" aria-hidden="true" />
              {FUEL_TYPE_LABELS[station.fuel_type] ?? station.fuel_type}
            </span>
            {station.location_name && (
              <span className="flex items-center gap-1">
                <MapPin className="w-3.5 h-3.5" aria-hidden="true" />
                {station.location_name}
              </span>
            )}
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium ${statusCfg.bg} ${statusCfg.color}`}
            >
              {statusCfg.label}
            </span>
          </div>
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded-md hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
            aria-label="Close station detail"
          >
            <X className="w-5 h-5" />
          </button>
        )}
      </div>

      {/* Stock overview */}
      <div className="p-4 border-b border-gray-100">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-medium text-gray-700">Stock Level</span>
          <span className="text-sm text-gray-500">
            {formatGallons(getCurrentStockGallons(station))} /{" "}
            {formatGallons(getCapacityGallons(station))}
          </span>
        </div>
        <div
          className="w-full h-3 bg-gray-200 rounded-full overflow-hidden"
          role="progressbar"
          aria-valuenow={Math.round(stockPct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`Stock level ${stockPct.toFixed(1)}%`}
        >
          <div
            className={`h-full rounded-full transition-all ${barColor}`}
            style={{ width: `${Math.min(stockPct, 100)}%` }}
          />
        </div>
        <div className="text-right text-xs text-gray-400 mt-1">
          {stockPct.toFixed(1)}%
        </div>

        {/* Key metrics */}
        <div className="grid grid-cols-3 gap-4 mt-4">
          <div className="text-center">
            <div className="flex items-center justify-center gap-1">
              <TrendingDown
                className="w-3.5 h-3.5 text-gray-400"
                aria-hidden="true"
              />
              <span className="text-lg font-semibold text-primary">
                {station.daily_consumption_rate > 0
                  ? formatGallons(
                      getFuelStationDailyConsumptionGallons(station),
                    )
                  : "—"}
              </span>
            </div>
            <div className="text-xs text-gray-500">Daily Rate</div>
          </div>
          <div className="text-center">
            <div className="flex items-center justify-center gap-1">
              <Clock className="w-3.5 h-3.5 text-gray-400" aria-hidden="true" />
              <span className="text-lg font-semibold text-primary">
                {station.days_until_empty > 0
                  ? `${station.days_until_empty.toFixed(1)}d`
                  : "—"}
              </span>
            </div>
            <div className="text-xs text-gray-500">Days Left</div>
          </div>
          <div className="text-center">
            <div className="flex items-center justify-center gap-1">
              <Droplets
                className="w-3.5 h-3.5 text-gray-400"
                aria-hidden="true"
              />
              <span className="text-lg font-semibold text-primary">
                {station.alert_threshold_pct}%
              </span>
            </div>
            <div className="text-xs text-gray-500">Alert Threshold</div>
          </div>
        </div>
      </div>

      {/* Action buttons */}
      <div className="px-4 pb-2 pt-3 border-b border-gray-100 flex gap-2">
        <button
          type="button"
          onClick={() =>
            setActiveForm(activeForm === "consumption" ? null : "consumption")
          }
          className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium rounded-lg transition-colors ${
            activeForm === "consumption"
              ? "bg-error text-white"
              : "bg-error-light text-error-dark hover:bg-error-light"
          }`}
        >
          <ArrowDown className="w-3.5 h-3.5" aria-hidden="true" />
          Record Consumption
        </button>
        <button
          type="button"
          onClick={() =>
            setActiveForm(activeForm === "refill" ? null : "refill")
          }
          className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium rounded-lg transition-colors ${
            activeForm === "refill"
              ? "bg-success text-white"
              : "bg-success-light text-success-dark hover:bg-success-light"
          }`}
        >
          <ArrowUp className="w-3.5 h-3.5" aria-hidden="true" />
          Record Refill
        </button>
      </div>

      {/* Inline event form */}
      {activeForm && (
        <div className="px-4 py-3 border-b border-gray-100">
          <FuelEventForm
            station={station}
            mode={activeForm}
            onClose={() => setActiveForm(null)}
            onSuccess={() => {
              setActiveForm(null);
              onEventRecorded?.();
            }}
          />
        </div>
      )}

      {/* Recent events */}
      <div className="p-4">
        <h4 className="text-sm font-medium text-gray-700 mb-3">
          Recent Events
        </h4>

        {recent_consumption_events.length === 0 &&
        recent_refill_events.length === 0 ? (
          <p className="text-sm text-gray-400 text-center py-4">
            No recent events
          </p>
        ) : (
          <div className="space-y-2 max-h-64 overflow-y-auto">
            {/* Merge and sort events by type for display */}
            {recent_consumption_events.map((evt, i) => (
              <div
                key={`consumption-${evt.asset_id}-${i}`}
                className="flex items-center gap-3 p-2 rounded-lg bg-error-light"
              >
                <ArrowDown
                  className="w-4 h-4 text-error flex-shrink-0"
                  aria-hidden="true"
                />
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-gray-700">
                    <span className="font-medium">Consumption</span>
                    {" — "}
                    {formatGallons(getEventQuantityGallons(evt))} to{" "}
                    {evt.asset_id}
                  </div>
                  <div className="text-xs text-gray-400">
                    Operator: {evt.operator_id}
                    {evt.odometer_reading != null &&
                      ` · Odometer: ${evt.odometer_reading} km`}
                  </div>
                </div>
              </div>
            ))}
            {recent_refill_events.map((evt, i) => (
              <div
                key={`refill-${evt.supplier}-${i}`}
                className="flex items-center gap-3 p-2 rounded-lg bg-success-light"
              >
                <ArrowUp
                  className="w-4 h-4 text-success flex-shrink-0"
                  aria-hidden="true"
                />
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-gray-700">
                    <span className="font-medium">Refill</span>
                    {" — "}
                    {formatGallons(getEventQuantityGallons(evt))} from{" "}
                    {evt.supplier}
                  </div>
                  <div className="text-xs text-gray-400">
                    Operator: {evt.operator_id}
                    {evt.delivery_reference &&
                      ` · Ref: ${evt.delivery_reference}`}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
