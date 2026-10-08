"use client";

import { ArrowDown, ArrowUp } from "lucide-react";
import { useState } from "react";
import { gallons } from "../../lib/format";
import type { FuelStation } from "../../services/fuelApi";
import {
  getFuelStationCapacityGallons,
  getFuelStationCurrentStockGallons,
  recordConsumption,
  recordRefill,
} from "../../services/fuelApi";
import {
  Button,
  Field,
  FormGrid,
  INPUT_CLASS,
  InlineBanner,
  NumberField,
} from "../ui";

type EventMode = "consumption" | "refill";

interface FuelEventFormProps {
  station: FuelStation;
  mode: EventMode;
  onClose: () => void;
  onSuccess: () => void;
}

/**
 * Inline form, inside the station drawer, for recording a fuel consumption
 * (dispensing) or refill (delivery) event against the station. Gallons use
 * `NumberField` (locale-aware, one decimal, max = stock or free capacity).
 *
 * On success, calls onSuccess so the parent can refresh station detail.
 */
export default function FuelEventForm({
  station,
  mode,
  onClose,
  onSuccess,
}: FuelEventFormProps) {
  const [quantity, setQuantity] = useState<number | null>(null);
  const [assetId, setAssetId] = useState("");
  const [operatorId, setOperatorId] = useState("");
  const [odometer, setOdometer] = useState<number | null>(null);
  const [supplier, setSupplier] = useState("");
  const [deliveryRef, setDeliveryRef] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isConsumption = mode === "consumption";
  const stock = getFuelStationCurrentStockGallons(station);
  const maxQuantity = Math.max(
    0,
    Math.floor(
      isConsumption ? stock : getFuelStationCapacityGallons(station) - stock,
    ),
  );

  const canSubmit =
    quantity !== null &&
    !Number.isNaN(quantity) &&
    quantity > 0 &&
    operatorId.trim() !== "" &&
    (isConsumption ? assetId.trim() !== "" : supplier.trim() !== "");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit || quantity === null) return;
    setSubmitting(true);
    setError(null);
    try {
      if (isConsumption) {
        await recordConsumption({
          station_id: station.station_id,
          fuel_type: station.fuel_type,
          quantity_gallons: quantity,
          asset_id: assetId.trim(),
          operator_id: operatorId.trim(),
          odometer_reading:
            odometer !== null && !Number.isNaN(odometer) ? odometer : undefined,
        });
      } else {
        await recordRefill({
          station_id: station.station_id,
          fuel_type: station.fuel_type,
          quantity_gallons: quantity,
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
  };

  const title = isConsumption ? "Record consumption" : "Record refill";
  return (
    <form
      onSubmit={handleSubmit}
      aria-label={title}
      className="rounded-lg border border-slate-200 bg-slate-50 p-3"
    >
      <h4 className="mb-3 flex items-center gap-2 text-sm font-semibold text-text">
        {isConsumption ? (
          <ArrowDown className="h-4 w-4 text-orange-700" aria-hidden="true" />
        ) : (
          <ArrowUp className="h-4 w-4 text-brand-700" aria-hidden="true" />
        )}
        {title}
      </h4>
      <FormGrid>
        <Field
          label="Quantity"
          required
          help={`Up to ${gallons(maxQuantity)}`}
          id="fuel-qty"
          span={1}
        >
          <NumberField
            id="fuel-qty"
            unit="gal"
            decimals={1}
            min={0.1}
            max={maxQuantity > 0 ? maxQuantity : undefined}
            value={quantity}
            onChange={setQuantity}
          />
        </Field>
        {isConsumption ? (
          <>
            <Field label="Asset / truck ID" required span={1}>
              <input
                id="fuel-asset"
                type="text"
                value={assetId}
                onChange={(e) => setAssetId(e.target.value)}
                placeholder="e.g. TRK-042"
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="Odometer" id="fuel-odometer" span={1}>
              <NumberField
                id="fuel-odometer"
                unit="km"
                min={0}
                value={odometer}
                onChange={setOdometer}
                placeholder="Optional"
              />
            </Field>
          </>
        ) : (
          <>
            <Field label="Supplier" required span={1}>
              <input
                id="fuel-supplier"
                type="text"
                value={supplier}
                onChange={(e) => setSupplier(e.target.value)}
                placeholder="e.g. PetroCorp"
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="Delivery reference" span={1}>
              <input
                id="fuel-ref"
                type="text"
                value={deliveryRef}
                onChange={(e) => setDeliveryRef(e.target.value)}
                placeholder="Optional"
                className={INPUT_CLASS}
              />
            </Field>
          </>
        )}
        <Field label="Operator ID" required span={1}>
          <input
            id="fuel-operator"
            type="text"
            value={operatorId}
            onChange={(e) => setOperatorId(e.target.value)}
            placeholder="e.g. OP-001"
            className={INPUT_CLASS}
          />
        </Field>
      </FormGrid>
      {error && (
        <InlineBanner tone="critical" className="mt-3">
          {error}
        </InlineBanner>
      )}
      <div className="mt-3 flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onClose}>
          Cancel
        </Button>
        <Button
          type="submit"
          size="sm"
          disabled={!canSubmit}
          loading={submitting}
        >
          {title}
        </Button>
      </div>
    </form>
  );
}
