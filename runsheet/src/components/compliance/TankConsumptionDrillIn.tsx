"use client";

/**
 * Per-tank consumption / forecast drill-in, launched from a K-Factor
 * calibration row. Gives the approver the consumption context behind the
 * variance — the tank's recent run-out forecasts (the consumption-model
 * output the K-factor feeds) — without leaving the calibration screen.
 *
 * This is intentionally a *per-tank* view (distinct from the network-level
 * Consumption tab): it answers "how has this specific tank's forecast
 * behaved?" so the operator can judge whether the suggested K is sensible.
 *
 * Data: GET /api/fuel/mvp/forecasts?customer_tank_id=… (newest first).
 */

import { useEffect, useState } from "react";
import type { KFactorEntry } from "../../services/complianceApi";
import type { CustomerTankForecast } from "../../services/fuelApi";
import { listCustomerTankForecasts } from "../../services/fuelApi";
import { getCurrentTenantId } from "../../services/tenant";

const MAX_ROWS = 20;

function fmtHours(h: number | null | undefined): string {
  if (h === null || h === undefined) return "—";
  if (h >= 24) return `${(h / 24).toFixed(1)} d`;
  return `${h.toFixed(1)} h`;
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${(v * 100).toFixed(0)}%`;
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function fmtK(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(4);
}

export default function TankConsumptionDrillIn({
  entry,
  onClose,
}: {
  entry: KFactorEntry;
  onClose: () => void;
}) {
  const [forecasts, setForecasts] = useState<CustomerTankForecast[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const resp = await listCustomerTankForecasts({
          tenant_id: getCurrentTenantId(),
          customer_tank_id: entry.tank_id,
          size: MAX_ROWS,
        });
        if (!cancelled) setForecasts(resp.data ?? []);
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Failed to load forecasts",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [entry.tank_id]);

  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="tank-consumption-title"
    >
      <div className="bg-white rounded-lg shadow-xl p-6 max-w-3xl w-full mx-4 max-h-[85vh] overflow-y-auto">
        <div className="flex items-start justify-between mb-4">
          <div>
            <h2 id="tank-consumption-title" className="text-lg font-bold">
              Consumption & Forecast — {entry.tank_id}
            </h2>
            <p className="text-sm text-gray-500">
              Recent run-out forecasts driven by this tank's K-factor.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-1.5 text-gray-500 hover:bg-gray-100"
          >
            ✕
          </button>
        </div>

        {/* Calibration summary from the row entry */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-5">
          <Stat label="Current K" value={fmtK(entry.current_kfactor)} mono />
          <Stat
            label="Suggested K"
            value={fmtK(entry.suggested_kfactor)}
            mono
          />
          <Stat
            label="Variance"
            value={
              entry.variance_percent === null
                ? "—"
                : `${entry.variance_percent >= 0 ? "+" : ""}${entry.variance_percent.toFixed(1)}%`
            }
          />
          <Stat label="Deliveries" value={String(entry.delivery_count)} />
        </div>

        {loading && (
          <div role="status" className="flex justify-center py-10">
            <span className="sr-only">Loading forecasts…</span>
            <div className="animate-spin h-7 w-7 border-4 border-primary border-t-transparent rounded-full" />
          </div>
        )}

        {!loading && error && (
          <div
            role="alert"
            className="bg-error-light border border-error-light text-error-dark p-3 rounded text-sm"
          >
            {error}
          </div>
        )}

        {!loading && !error && forecasts.length === 0 && (
          <p className="text-sm text-gray-500 py-8 text-center">
            No forecasts recorded for this tank yet.
          </p>
        )}

        {!loading && !error && forecasts.length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 border-b border-gray-200">
                <th className="py-2 pr-3 font-medium">When</th>
                <th className="py-2 pr-3 font-medium">Model</th>
                <th className="py-2 pr-3 font-medium">Run-out (p50)</th>
                <th className="py-2 pr-3 font-medium">Run-out (p90)</th>
                <th className="py-2 pr-3 font-medium">24h risk</th>
                <th className="py-2 pr-3 font-medium">Confidence</th>
                <th className="py-2 font-medium">Flags</th>
              </tr>
            </thead>
            <tbody>
              {forecasts.map((f, i) => (
                <tr
                  key={f.forecast_id ?? `${f.timestamp}-${i}`}
                  className="border-b border-gray-100"
                >
                  <td className="py-2 pr-3 whitespace-nowrap">
                    {fmtTime(f.timestamp)}
                  </td>
                  <td className="py-2 pr-3 text-gray-600">
                    {f.model_name ?? "—"}
                  </td>
                  <td className="py-2 pr-3">
                    {fmtHours(f.hours_to_runout_p50)}
                  </td>
                  <td className="py-2 pr-3">
                    {fmtHours(f.hours_to_runout_p90)}
                  </td>
                  <td className="py-2 pr-3">
                    <span
                      className={
                        (f.runout_risk_24h ?? 0) >= 0.5
                          ? "text-error font-medium"
                          : "text-gray-700"
                      }
                    >
                      {fmtPct(f.runout_risk_24h)}
                    </span>
                  </td>
                  <td className="py-2 pr-3">{fmtPct(f.confidence)}</td>
                  <td className="py-2 text-xs text-gray-500">
                    {f.anomaly_flags && f.anomaly_flags.length > 0
                      ? f.anomaly_flags.join(", ")
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="bg-gray-50 border border-gray-200 rounded-lg p-3">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`mt-0.5 font-semibold ${mono ? "font-mono" : ""}`}>
        {value}
      </div>
    </div>
  );
}
