"use client";

import { ArrowDown, ArrowUp, Loader2, X } from "lucide-react";
import { useCallback, useState } from "react";
import type { FuelStation } from "../../services/fuelApi";
import {
  gallonsToLiters,
  getFuelStationCapacityGallons,
  getFuelStationCurrentStockGallons,
  recordConsumption,
  recordRefill,
} from "../../services/fuelApi";

type EventMode = "consumption" | "refill";

interface FuelEventFormProps {
  station: FuelStation;
  mode: EventMode;
  onClose: () => void;
  onSuccess: () => void;
}

function getCapacityGallons(station: FuelStation): number {
  return getFuelStationCapacityGallons(station);
}

function getCurrentStockGallons(station: FuelStation): number {
  return getFuelStationCurrentStockGallons(station);
}

/**
 * Inline form for recording a fuel consumption (dispensing) or refill
 * (delivery) event against a specific station.
 *
 * On success, calls onSuccess so the parent can refresh station detail.
 */
export default function FuelEventForm({
  station,
  mode,
  onClose,
  onSuccess,
}: FuelEventFormProps) {
  const [quantity, setQuantity] = useState("");
  const [assetId, setAssetId] = useState("");
  const [operatorId, setOperatorId] = useState("");
  const [odometer, setOdometer] = useState("");
  const [supplier, setSupplier] = useState("");
  const [deliveryRef, setDeliveryRef] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isConsumption = mode === "consumption";
  const maxQuantity = isConsumption
    ? getCurrentStockGallons(station)
    : getCapacityGallons(station) - getCurrentStockGallons(station);

  const canSubmit =
    Number(quantity) > 0 &&
    operatorId.trim() !== "" &&
    (isConsumption ? assetId.trim() !== "" : supplier.trim() !== "");

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!canSubmit) return;

      setSubmitting(true);
      setError(null);

      try {
        if (isConsumption) {
          await recordConsumption({
            station_id: station.station_id,
            fuel_type: station.fuel_type,
            quantity_liters: gallonsToLiters(Number(quantity)),
            asset_id: assetId.trim(),
            operator_id: operatorId.trim(),
            odometer_reading: odometer ? Number(odometer) : undefined,
          });
        } else {
          await recordRefill({
            station_id: station.station_id,
            fuel_type: station.fuel_type,
            quantity_liters: gallonsToLiters(Number(quantity)),
            supplier: supplier.trim(),
            operator_id: operatorId.trim(),
            delivery_reference: deliveryRef.trim() || undefined,
          });
        }
        onSuccess();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to record event");
      } finally {
        setSubmitting(false);
      }
    },
    [
      canSubmit,
      isConsumption,
      station,
      quantity,
      assetId,
      operatorId,
      odometer,
      supplier,
      deliveryRef,
      onSuccess,
    ],
  );

  return (
    <form
      onSubmit={handleSubmit}
      className="border border-gray-200 rounded-lg bg-gray-50 p-4"
    >
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          {isConsumption ? (
            <ArrowDown className="w-4 h-4 text-error" aria-hidden="true" />
          ) : (
            <ArrowUp className="w-4 h-4 text-success" aria-hidden="true" />
          )}
          <h4 className="text-sm font-semibold text-primary">
            {isConsumption ? "Record Consumption" : "Record Refill"}
          </h4>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="p-1 rounded hover:bg-gray-200 text-gray-400 hover:text-gray-600 transition-colors"
          aria-label="Cancel"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="space-y-3">
        {/* Quantity */}
        <div>
          <label
            htmlFor="fuel-qty"
            className="block text-xs font-medium text-gray-600 mb-1"
          >
            Quantity (gallons) *
          </label>
          <input
            id="fuel-qty"
            type="number"
            min="1"
            max={maxQuantity > 0 ? maxQuantity : undefined}
            step="0.1"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            placeholder={`Max ${maxQuantity.toLocaleString()} gal`}
            className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            required
          />
        </div>

        {/* Consumption-specific fields */}
        {isConsumption && (
          <>
            <div>
              <label
                htmlFor="fuel-asset"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Asset / Truck ID *
              </label>
              <input
                id="fuel-asset"
                type="text"
                value={assetId}
                onChange={(e) => setAssetId(e.target.value)}
                placeholder="e.g. TRK-042"
                className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
                required
              />
            </div>
            <div>
              <label
                htmlFor="fuel-odometer"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Odometer (km)
              </label>
              <input
                id="fuel-odometer"
                type="number"
                min="0"
                step="1"
                value={odometer}
                onChange={(e) => setOdometer(e.target.value)}
                placeholder="Optional"
                className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
              />
            </div>
          </>
        )}

        {/* Refill-specific fields */}
        {!isConsumption && (
          <>
            <div>
              <label
                htmlFor="fuel-supplier"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Supplier *
              </label>
              <input
                id="fuel-supplier"
                type="text"
                value={supplier}
                onChange={(e) => setSupplier(e.target.value)}
                placeholder="e.g. PetroCorp"
                className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
                required
              />
            </div>
            <div>
              <label
                htmlFor="fuel-ref"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Delivery Reference
              </label>
              <input
                id="fuel-ref"
                type="text"
                value={deliveryRef}
                onChange={(e) => setDeliveryRef(e.target.value)}
                placeholder="Optional"
                className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
              />
            </div>
          </>
        )}

        {/* Operator */}
        <div>
          <label
            htmlFor="fuel-operator"
            className="block text-xs font-medium text-gray-600 mb-1"
          >
            Operator ID *
          </label>
          <input
            id="fuel-operator"
            type="text"
            value={operatorId}
            onChange={(e) => setOperatorId(e.target.value)}
            placeholder="e.g. OP-001"
            className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            required
          />
        </div>

        {/* Error */}
        {error && (
          <p className="text-xs text-error" role="alert">
            {error}
          </p>
        )}

        {/* Submit */}
        <button
          type="submit"
          disabled={!canSubmit || submitting}
          className={`w-full flex items-center justify-center gap-2 px-4 py-2 text-sm font-medium rounded-lg transition-colors ${
            isConsumption
              ? "bg-error hover:bg-error-dark text-white"
              : "bg-success hover:bg-success-dark text-white"
          } disabled:opacity-50 disabled:cursor-not-allowed`}
        >
          {submitting && <Loader2 className="w-4 h-4 animate-spin" />}
          {submitting
            ? "Recording..."
            : isConsumption
              ? "Record Consumption"
              : "Record Refill"}
        </button>
      </div>
    </form>
  );
}
