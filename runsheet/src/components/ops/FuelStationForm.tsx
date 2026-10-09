"use client";

/**
 * FuelStationForm: create and edit a fuel station, as a `FormDialog` (UI
 * revamp R6, design.md §5 "Fuel station": md, NumberField, ProductSelect).
 *
 * This is the dialog the owner picked as the create/edit standard. It also
 * fixes the two bugs reported in it (audit §d):
 *
 * - Capacity showed `5283,441047162968`: a litre-based station's capacity was
 *   converted to gallons unrounded and put into a `type=number` input. Gallons
 *   are now rounded to whole numbers on load and edited with `NumberField`
 *   (locale-aware display and parsing, whole gallons, min/max).
 * - Fuel Type read `GASOLINE_REG (Regular U…`: options were `CODE (Name)`.
 *   `ProductSelect` shows the RP 1637 cap and the readable name, with the
 *   code as secondary text, sized to the longest label.
 *
 * Modes:
 * - create: POST /fuel/stations
 * - edit: PATCH /fuel/stations/{id}; when only the threshold changed, the
 *   dedicated PATCH /fuel/stations/{id}/threshold
 *
 * `validateStationForm` is exported for independent testing.
 */

import type {
  CreateStationPayload,
  FuelStation,
  FuelType,
  UpdateStationPayload,
} from "../../services/fuelApi";
import {
  createStation,
  getFuelStationCapacityGallons,
  updateStation,
  updateStationThreshold,
} from "../../services/fuelApi";
import { getCurrentTenantId } from "../../services/tenant";
import {
  Field,
  FormDialog,
  INPUT_CLASS,
  NumberField,
  ProductSelect,
  validateNumber,
} from "../ui";

// ─── Constants ───────────────────────────────────────────────────────────────

/** Products a station can hold (the backend `FuelType` enum). */
export const STATION_FUEL_TYPES: FuelType[] = [
  "DIESEL_2",
  "GASOLINE_REG",
  "GASOLINE_PREM",
  "HEATING_OIL",
  "PROPANE",
  "KEROSENE",
  "OFF_ROAD_DIESEL",
  "DEF",
];

/** Station capacity in whole gallons (litre-based stations are converted). */
export function stationCapacityGallons(station?: FuelStation | null): number {
  return Math.round(getFuelStationCapacityGallons(station));
}

// ─── Validation ──────────────────────────────────────────────────────────────

export interface StationFormValues {
  name: string;
  fuel_type: FuelType;
  capacity_gallons: number | null;
  initial_stock_gallons: number | null;
  location_name: string;
  alert_threshold_pct: number | null;
}

export interface ValidationErrors {
  name?: string;
  capacity_gallons?: string;
  initial_stock_gallons?: string;
  alert_threshold_pct?: string;
}

/**
 * Pure validation for station form values. Returns field-level messages, or
 * an empty object if valid.
 *
 * - name must be non-empty
 * - capacity_gallons must be a positive whole number of gallons
 * - initial_stock_gallons (create) must be 0 to capacity
 * - alert_threshold_pct must be between 0 and 100 (inclusive)
 */
export function validateStationForm(
  values: StationFormValues,
): ValidationErrors {
  const errors: ValidationErrors = {};

  if (!values.name || values.name.trim() === "") {
    errors.name = "Station name is required.";
  }

  const cap = values.capacity_gallons;
  if (cap === null || cap === undefined || Number.isNaN(cap) || cap <= 0) {
    errors.capacity_gallons = "Capacity must be a positive number.";
  }

  const stock = values.initial_stock_gallons;
  if (stock !== null && stock !== undefined) {
    const msg = validateNumber(stock, {
      min: 0,
      max: cap && cap > 0 ? cap : undefined,
    });
    if (msg) errors.initial_stock_gallons = msg;
  }

  const t = values.alert_threshold_pct;
  if (t === null || t === undefined || Number.isNaN(t) || t < 0 || t > 100) {
    errors.alert_threshold_pct = "Threshold must be between 0 and 100.";
  }

  return errors;
}

// ─── Component ───────────────────────────────────────────────────────────────

interface FuelStationFormProps {
  mode: "create" | "edit";
  /** Station data for edit mode pre-population */
  station?: FuelStation | null;
  onClose: () => void;
  onSuccess: (station: FuelStation) => void;
}

type Values = StationFormValues & Record<string, unknown>;

export function initialStationValues(
  station?: FuelStation | null,
): StationFormValues {
  return {
    name: station?.name ?? "",
    fuel_type: station?.fuel_type ?? "DIESEL_2",
    capacity_gallons: station ? stationCapacityGallons(station) : null,
    initial_stock_gallons: station ? null : 0,
    location_name: station?.location_name ?? "",
    alert_threshold_pct: station?.alert_threshold_pct ?? 20,
  };
}

export default function FuelStationForm({
  mode,
  station,
  onClose,
  onSuccess,
}: FuelStationFormProps) {
  const tenantId = getCurrentTenantId();
  const initial = initialStationValues(station) as Values;

  const isThresholdOnlyChange = (form: StationFormValues) =>
    mode === "edit" &&
    !!station &&
    form.name === station.name &&
    form.fuel_type === station.fuel_type &&
    form.capacity_gallons === initial.capacity_gallons &&
    form.location_name === (station.location_name ?? "") &&
    form.alert_threshold_pct !== station.alert_threshold_pct;

  const submit = async (form: StationFormValues): Promise<FuelStation> => {
    const name = form.name.trim();
    const location = form.location_name.trim();
    const capacity = form.capacity_gallons as number;
    const threshold = form.alert_threshold_pct as number;
    if (mode === "create") {
      const payload: CreateStationPayload = {
        station_id: `FS-${Date.now().toString(36).toUpperCase()}`,
        name,
        fuel_type: form.fuel_type,
        capacity_gallons: capacity,
        initial_stock_gallons: form.initial_stock_gallons ?? 0,
        alert_threshold_pct: threshold,
      };
      if (location) payload.location_name = location;
      return createStation(payload, tenantId);
    }
    if (!station) throw new Error("Station data is required for edit mode.");
    if (isThresholdOnlyChange(form)) {
      return updateStationThreshold(station.station_id, threshold, tenantId);
    }
    const payload: UpdateStationPayload = {
      name,
      fuel_type: form.fuel_type,
      alert_threshold_pct: threshold,
    };
    // Only send capacity when it changed, so opening and saving a litre-based
    // station doesn't rewrite its capacity with the rounded gallon figure.
    if (form.capacity_gallons !== initial.capacity_gallons) {
      payload.capacity_gallons = capacity;
    }
    if (location) payload.location_name = location;
    return updateStation(station.station_id, payload, tenantId);
  };

  return (
    <FormDialog<Values, FuelStation>
      open
      size="md"
      title={mode === "create" ? "Add fuel station" : "Edit fuel station"}
      submitLabel={mode === "create" ? "Create station" : "Save changes"}
      successMessage={mode === "create" ? "Station created" : "Station saved"}
      initialValues={initial}
      validate={(v) => ({ ...validateStationForm(v) })}
      onSubmit={submit}
      onSaved={onSuccess}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field label="Station name" required error={errors.name}>
            <input
              id="station-name"
              type="text"
              value={values.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="e.g. Houston Main Terminal"
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="Fuel type" required id="fuel-type">
            <ProductSelect
              id="fuel-type"
              value={values.fuel_type}
              options={STATION_FUEL_TYPES}
              onChange={(code) => set("fuel_type", code as FuelType)}
            />
          </Field>
          <Field
            label="Capacity"
            required
            error={errors.capacity_gallons}
            span={1}
            id="capacity-gallons"
          >
            <NumberField
              id="capacity-gallons"
              unit="gal"
              min={1}
              value={values.capacity_gallons}
              onChange={(n) => set("capacity_gallons", n)}
              placeholder="50,000"
            />
          </Field>
          {mode === "create" && (
            <Field
              label="Initial stock"
              error={errors.initial_stock_gallons}
              span={1}
              id="initial-stock"
            >
              <NumberField
                id="initial-stock"
                unit="gal"
                min={0}
                max={values.capacity_gallons ?? undefined}
                value={values.initial_stock_gallons}
                onChange={(n) => set("initial_stock_gallons", n)}
                placeholder="30,000"
              />
            </Field>
          )}
          <Field label="Location name" span={mode === "create" ? 2 : 1}>
            <input
              id="location-name"
              type="text"
              value={values.location_name}
              onChange={(e) => set("location_name", e.target.value)}
              placeholder="e.g. Industrial District"
              className={INPUT_CLASS}
            />
          </Field>
          <Field
            label="Alert threshold"
            required
            help="Alert when stock falls below this percentage of capacity."
            error={errors.alert_threshold_pct}
            id="alert-threshold"
          >
            <NumberField
              id="alert-threshold"
              unit="%"
              min={0}
              max={100}
              value={values.alert_threshold_pct}
              onChange={(n) => set("alert_threshold_pct", n)}
            />
          </Field>
        </>
      )}
    </FormDialog>
  );
}
