"use client";

import { ArrowDown, ArrowUp, Clock, Droplets, Fuel, MapPin, TrendingDown, X } from "lucide-react";
import type {
  ConsumptionEvent,
  FuelStation,
  FuelStationDetail as FuelStationDetailType,
  FuelType,
  RefillEvent,
  StationStatus,
} from "../../services/fuelApi";

interface FuelStationDetailProps {
  detail: FuelStationDetailType;
  onClose?: () => void;
}

const STATUS_CONFIG: Record<StationStatus, { label: string; color: string; bg: string }> = {
  normal:   { label: "Normal",   color: "text-green-700",  bg: "bg-green-100" },
  low:      { label: "Low",      color: "text-yellow-700", bg: "bg-yellow-100" },
  critical: { label: "Critical", color: "text-red-700",    bg: "bg-red-100" },
  empty:    { label: "Empty",    color: "text-gray-700",   bg: "bg-gray-100" },
};

const STATUS_BAR_COLORS: Record<StationStatus, string> = {
  normal: "bg-green-500",
  low: "bg-yellow-500",
  critical: "bg-red-500",
  empty: "bg-gray-400",
};

const FUEL_TYPE_LABELS: Record<FuelType, string> = {
  AGO: "AGO (Diesel)",
  PMS: "PMS (Petrol)",
  ATK: "ATK (Aviation)",
  LPG: "LPG (Gas)",
};

function formatLiters(liters: number): string {
  if (liters >= 1_000_000) return `${(liters / 1_000_000).toFixed(1)}M L`;
  if (liters >= 1_000) return `${(liters / 1_000).toFixed(1)}K L`;
  return `${liters.toFixed(0)} L`;
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return ts;
  }
}

/**
 * Station detail panel showing stock level, recent consumption events,
 * recent refill events, and daily consumption rate.
 *
 * Validates: Requirements 6.6
 */
export default function FuelStationDetail({ detail, onClose }: FuelStationDetailProps) {
  const { station, recent_consumption_events, recent_refill_events } = detail;
  const stockPct =
    station.capacity_liters > 0
      ? (station.current_stock_liters / station.capacity_liters) * 100
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
          <h3 className="text-lg font-semibold text-[#232323]">{station.name}</h3>
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
            <span className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium ${statusCfg.bg} ${statusCfg.color}`}>
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
            {formatLiters(station.current_stock_liters)} / {formatLiters(station.capacity_liters)}
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
        <div className="text-right text-xs text-gray-400 mt-1">{stockPct.toFixed(1)}%</div>

        {/* Key metrics */}
        <div className="grid grid-cols-3 gap-4 mt-4">
          <div className="text-center">
            <div className="flex items-center justify-center gap-1">
              <TrendingDown className="w-3.5 h-3.5 text-gray-400" aria-hidden="true" />
              <span className="text-lg font-semibold text-[#232323]">
                {station.daily_consumption_rate > 0
                  ? formatLiters(station.daily_consumption_rate)
                  : "—"}
              </span>
            </div>
            <div className="text-xs text-gray-500">Daily Rate</div>
          </div>
          <div className="text-center">
            <div className="flex items-center justify-center gap-1">
              <Clock className="w-3.5 h-3.5 text-gray-400" aria-hidden="true" />
              <span className="text-lg font-semibold text-[#232323]">
                {station.days_until_empty > 0
                  ? `${station.days_until_empty.toFixed(1)}d`
                  : "—"}
              </span>
            </div>
            <div className="text-xs text-gray-500">Days Left</div>
          </div>
          <div className="text-center">
            <div className="flex items-center justify-center gap-1">
              <Droplets className="w-3.5 h-3.5 text-gray-400" aria-hidden="true" />
              <span className="text-lg font-semibold text-[#232323]">
                {station.alert_threshold_pct}%
              </span>
            </div>
            <div className="text-xs text-gray-500">Alert Threshold</div>
          </div>
        </div>
      </div>

      {/* Recent events */}
      <div className="p-4">
        <h4 className="text-sm font-medium text-gray-700 mb-3">Recent Events</h4>

        {recent_consumption_events.length === 0 && recent_refill_events.length === 0 ? (
          <p className="text-sm text-gray-400 text-center py-4">No recent events</p>
        ) : (
          <div className="space-y-2 max-h-64 overflow-y-auto">
            {/* Merge and sort events by type for display */}
            {recent_consumption_events.map((evt, i) => (
              <div
                key={`consumption-${evt.asset_id}-${i}`}
                className="flex items-center gap-3 p-2 rounded-lg bg-red-50"
              >
                <ArrowDown className="w-4 h-4 text-red-500 flex-shrink-0" aria-hidden="true" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-gray-700">
                    <span className="font-medium">Consumption</span>
                    {" — "}
                    {formatLiters(evt.quantity_liters)} to {evt.asset_id}
                  </div>
                  <div className="text-xs text-gray-400">
                    Operator: {evt.operator_id}
                    {evt.odometer_reading != null && ` · Odometer: ${evt.odometer_reading} km`}
                  </div>
                </div>
              </div>
            ))}
            {recent_refill_events.map((evt, i) => (
              <div
                key={`refill-${evt.supplier}-${i}`}
                className="flex items-center gap-3 p-2 rounded-lg bg-green-50"
              >
                <ArrowUp className="w-4 h-4 text-green-500 flex-shrink-0" aria-hidden="true" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-gray-700">
                    <span className="font-medium">Refill</span>
                    {" — "}
                    {formatLiters(evt.quantity_liters)} from {evt.supplier}
                  </div>
                  <div className="text-xs text-gray-400">
                    Operator: {evt.operator_id}
                    {evt.delivery_reference && ` · Ref: ${evt.delivery_reference}`}
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
