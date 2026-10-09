"use client";

import {
  ArrowDown,
  ArrowUp,
  Clock,
  Droplets,
  TrendingDown,
} from "lucide-react";
import { useState } from "react";
import { gallons, number, pct } from "../../lib/format";
import type { FuelStationDetail as FuelStationDetailType } from "../../services/fuelApi";
import {
  displayDaysUntilEmpty,
  getEventQuantityGallons,
  getFuelStationCapacityGallons,
  getFuelStationCurrentStockGallons,
  getFuelStationDailyConsumptionGallons,
} from "../../services/fuelApi";
import { STATUS } from "../../styles/tokens";
import { ProductChip, StatusBadge } from "../ui";
import FuelEventForm from "./FuelEventForm";
import { STATION_STATUS } from "./FuelStationList";

interface FuelStationDetailProps {
  detail: FuelStationDetailType;
  /** Kept for callers; the surrounding Drawer owns the close button. */
  onClose?: () => void;
  onEventRecorded?: () => void;
}

/**
 * Station detail (shown in the Stations drawer): product, status, stock bar
 * with gallons and percentage as text, daily rate, days left, threshold,
 * record consumption / refill, and recent events.
 *
 * Validates: Requirements 6.6
 */
export default function FuelStationDetail({
  detail,
  onEventRecorded,
}: FuelStationDetailProps) {
  const { station, recent_consumption_events, recent_refill_events } = detail;
  const [activeForm, setActiveForm] = useState<"consumption" | "refill" | null>(
    null,
  );
  const capacity = getFuelStationCapacityGallons(station);
  const stock = getFuelStationCurrentStockGallons(station);
  const stockPct = capacity > 0 ? (stock / capacity) * 100 : 0;
  const cfg = STATION_STATUS[station.status] ?? STATION_STATUS.normal;

  const metric = (icon: React.ReactNode, value: string, label: string) => (
    <div className="text-center">
      <div className="flex items-center justify-center gap-1">
        {icon}
        <span className="text-base font-semibold tabular-nums text-text">
          {value}
        </span>
      </div>
      <div className="text-xs text-text-muted">{label}</div>
    </div>
  );

  // null for a station with no consumption: shown as "—" (F5).
  const daysLeft = displayDaysUntilEmpty(station);

  return (
    <section
      className="flex flex-col gap-4"
      aria-label={`Station detail: ${station.name}`}
    >
      <header className="flex flex-col gap-1.5">
        <h3 className="text-base font-semibold text-text">{station.name}</h3>
        <div className="flex flex-wrap items-center gap-2 text-sm text-text-muted">
          <ProductChip code={station.fuel_type} variant="chip" />
          <StatusBadge status={cfg.status} label={cfg.label} />
          {station.location_name && <span>{station.location_name}</span>}
        </div>
      </header>

      <div>
        <div className="mb-1.5 flex items-center justify-between text-sm">
          <span className="font-medium text-text">Stock level</span>
          <span className="tabular-nums text-text-muted">
            {number(stock)} / {gallons(capacity)} · {pct(stockPct)}
          </span>
        </div>
        <div
          className="h-3 w-full overflow-hidden rounded-full bg-slate-200"
          role="progressbar"
          aria-valuenow={Math.round(stockPct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`Stock level ${pct(stockPct)}`}
        >
          <div
            className="h-full rounded-full"
            style={{
              width: `${Math.min(stockPct, 100)}%`,
              backgroundColor: STATUS[cfg.status].dot,
            }}
          />
        </div>
        <div className="mt-4 grid grid-cols-3 gap-4">
          {metric(
            <TrendingDown
              aria-hidden="true"
              className="h-3.5 w-3.5 text-slate-500"
            />,
            station.daily_consumption_rate > 0
              ? gallons(getFuelStationDailyConsumptionGallons(station))
              : "—",
            "Daily rate",
          )}
          {metric(
            <Clock aria-hidden="true" className="h-3.5 w-3.5 text-slate-500" />,
            daysLeft != null ? `${number(daysLeft, { decimals: 1 })} d` : "—",
            "Days left",
          )}
          {metric(
            <Droplets
              aria-hidden="true"
              className="h-3.5 w-3.5 text-slate-500"
            />,
            pct(station.alert_threshold_pct),
            "Alert threshold",
          )}
        </div>
      </div>

      <div className="flex gap-2">
        <button
          type="button"
          aria-pressed={activeForm === "consumption"}
          onClick={() =>
            setActiveForm(activeForm === "consumption" ? null : "consumption")
          }
          className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-xs font-semibold ${
            activeForm === "consumption"
              ? "border-orange-700 bg-orange-700 text-white"
              : "border-orange-300 bg-orange-50 text-orange-800 hover:bg-orange-100"
          }`}
        >
          <ArrowDown className="h-3.5 w-3.5" aria-hidden="true" />
          Record consumption
        </button>
        <button
          type="button"
          aria-pressed={activeForm === "refill"}
          onClick={() =>
            setActiveForm(activeForm === "refill" ? null : "refill")
          }
          className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-xs font-semibold ${
            activeForm === "refill"
              ? "border-brand-700 bg-brand-700 text-white"
              : "border-brand-300 bg-brand-50 text-brand-800 hover:bg-brand-100"
          }`}
        >
          <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
          Record refill
        </button>
      </div>

      {activeForm && (
        <FuelEventForm
          station={station}
          mode={activeForm}
          onClose={() => setActiveForm(null)}
          onSuccess={() => {
            setActiveForm(null);
            onEventRecorded?.();
          }}
        />
      )}

      <div>
        <h4 className="mb-2 text-sm font-semibold text-text">Recent events</h4>
        {recent_consumption_events.length === 0 &&
        recent_refill_events.length === 0 ? (
          <p className="py-4 text-center text-sm text-text-muted">
            No recent events
          </p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {recent_consumption_events.map((evt, i) => (
              <li
                key={`consumption-${evt.asset_id}-${i}`}
                className="flex items-center gap-3 rounded-lg border border-orange-300 bg-orange-50 p-2"
              >
                <ArrowDown
                  className="h-4 w-4 shrink-0 text-orange-700"
                  aria-hidden="true"
                />
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-text">
                    <span className="font-medium">Consumption</span>
                    {" · "}
                    {gallons(getEventQuantityGallons(evt))} to {evt.asset_id}
                  </div>
                  <div className="text-xs text-text-muted">
                    Operator: {evt.operator_id}
                    {evt.odometer_reading != null &&
                      ` · Odometer: ${number(evt.odometer_reading)} km`}
                  </div>
                </div>
              </li>
            ))}
            {recent_refill_events.map((evt, i) => (
              <li
                key={`refill-${evt.supplier}-${i}`}
                className="flex items-center gap-3 rounded-lg border border-brand-300 bg-brand-50 p-2"
              >
                <ArrowUp
                  className="h-4 w-4 shrink-0 text-brand-700"
                  aria-hidden="true"
                />
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-text">
                    <span className="font-medium">Refill</span>
                    {" · "}
                    {gallons(getEventQuantityGallons(evt))} from {evt.supplier}
                  </div>
                  <div className="text-xs text-text-muted">
                    Operator: {evt.operator_id}
                    {evt.delivery_reference &&
                      ` · Ref: ${evt.delivery_reference}`}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
