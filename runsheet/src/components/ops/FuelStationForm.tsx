"use client";

/**
 * FuelStationForm — Modal form for creating and editing fuel stations.
 *
 * Supports three modes:
 * - "create": Empty form, calls createStation on submit
 * - "edit": Pre-populated form, calls updateStation on submit
 * - Threshold-only edit: calls updateStationThreshold when only threshold changes
 *
 * Exports `validateStationForm` for independent testing (Property 2).
 *
 * Validates:
 * - Requirement 8.1: Creation form with name, fuel_type, capacity_liters, location, alert_threshold_pct
 * - Requirement 8.2: Calls POST /fuel/stations on create
 * - Requirement 8.3: Pre-populated edit form with current values
 * - Requirement 8.4: Calls PATCH /fuel/stations/{id} on edit
 * - Requirement 8.5: Calls PATCH /fuel/stations/{id}/threshold for threshold-only edit
 * - Requirement 8.6: Validates capacity > 0 and threshold 0-100
 * - Requirement 8.7: Displays API error and retains form values
 */

import { X } from "lucide-react";
import { useState } from "react";
import type {
  CreateStationPayload,
  FuelStation,
  FuelType,
  UpdateStationPayload,
} from "../../services/fuelApi";
import {
  createStation,
  updateStation,
  updateStationThreshold,
} from "../../services/fuelApi";

// ─── Constants ───────────────────────────────────────────────────────────────

const TENANT_ID = "default";

const FUEL_TYPES: { value: FuelType; label: string }[] = [
  { value: "AGO", label: "AGO (Diesel)" },
  { value: "PMS", label: "PMS (Petrol)" },
  { value: "ATK", label: "ATK (Aviation)" },
  { value: "LPG", label: "LPG (Gas)" },
];

// ─── Validation ──────────────────────────────────────────────────────────────

export interface StationFormValues {
  name: string;
  fuel_type: FuelType;
  capacity_liters: number;
  location_name: string;
  alert_threshold_pct: number;
}

export interface ValidationErrors {
  name?: string;
  capacity_liters?: string;
  alert_threshold_pct?: string;
}

/**
 * Pure validation function for fuel station form values.
 * Returns an object with field-level error messages, or an empty object if valid.
 *
 * Rules:
 * - name must be non-empty
 * - capacity_liters must be a positive number (> 0)
 * - alert_threshold_pct must be between 0 and 100 (inclusive)
 */
export function validateStationForm(values: StationFormValues): ValidationErrors {
  const errors: ValidationErrors = {};

  if (!values.name || values.name.trim() === "") {
    errors.name = "Station name is required.";
  }

  if (
    values.capacity_liters === null ||
    values.capacity_liters === undefined ||
    isNaN(values.capacity_liters) ||
    values.capacity_liters <= 0
  ) {
    errors.capacity_liters = "Capacity must be a positive number.";
  }

  if (
    values.alert_threshold_pct === null ||
    values.alert_threshold_pct === undefined ||
    isNaN(values.alert_threshold_pct) ||
    values.alert_threshold_pct < 0 ||
    values.alert_threshold_pct > 100
  ) {
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

export default function FuelStationForm({
  mode,
  station,
  onClose,
  onSuccess,
}: FuelStationFormProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState<ValidationErrors>({});

  const [form, setForm] = useState<StationFormValues>({
    name: station?.name ?? "",
    fuel_type: station?.fuel_type ?? "AGO",
    capacity_liters: station?.capacity_liters ?? 0,
    location_name: station?.location_name ?? "",
    alert_threshold_pct: station?.alert_threshold_pct ?? 20,
  });

  /**
   * Determine if only the threshold changed (edit mode).
   * If so, we use the dedicated threshold endpoint.
   */
  function isThresholdOnlyChange(): boolean {
    if (mode !== "edit" || !station) return false;
    return (
      form.name === station.name &&
      form.fuel_type === station.fuel_type &&
      form.capacity_liters === station.capacity_liters &&
      form.location_name === (station.location_name ?? "") &&
      form.alert_threshold_pct !== station.alert_threshold_pct
    );
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // Client-side validation
    const errors = validateStationForm(form);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) {
      return;
    }

    setError("");
    setSubmitting(true);

    try {
      let result: FuelStation;

      if (mode === "create") {
        const payload: CreateStationPayload = {
          name: form.name.trim(),
          fuel_type: form.fuel_type,
          capacity_liters: form.capacity_liters,
          alert_threshold_pct: form.alert_threshold_pct,
        };
        if (form.location_name.trim()) {
          payload.location_name = form.location_name.trim();
        }
        result = await createStation(payload, TENANT_ID);
      } else if (isThresholdOnlyChange()) {
        // Threshold-only edit uses the dedicated endpoint
        result = await updateStationThreshold(
          station!.station_id,
          form.alert_threshold_pct,
          TENANT_ID,
        );
      } else {
        // Full edit
        const payload: UpdateStationPayload = {
          name: form.name.trim(),
          fuel_type: form.fuel_type,
          capacity_liters: form.capacity_liters,
          alert_threshold_pct: form.alert_threshold_pct,
        };
        if (form.location_name.trim()) {
          payload.location_name = form.location_name.trim();
        }
        result = await updateStation(station!.station_id, payload, TENANT_ID);
      }

      onSuccess(result);
      onClose();
    } catch (err) {
      // Retain form values, display error (Requirement 8.7)
      setError(
        err instanceof Error ? err.message : "An unexpected error occurred",
      );
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  const errorInputClass =
    "w-full px-3 py-2 text-sm border border-red-300 rounded-lg focus:ring-2 focus:ring-red-200 focus:border-red-400 bg-white";

  const title = mode === "create" ? "Add Fuel Station" : "Edit Fuel Station";
  const submitLabel = mode === "create" ? "Create Station" : "Save Changes";
  const submittingLabel = mode === "create" ? "Creating..." : "Saving...";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-[#232323]">{title}</h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close form"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {/* API error banner */}
          {error && (
            <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
              {error}
            </p>
          )}

          {/* Station Name */}
          <div>
            <label
              htmlFor="station-name"
              className="block text-xs font-medium text-gray-600 mb-1"
            >
              Station Name
            </label>
            <input
              id="station-name"
              type="text"
              value={form.name}
              onChange={(e) => {
                setForm({ ...form, name: e.target.value });
                if (fieldErrors.name) {
                  setFieldErrors({ ...fieldErrors, name: undefined });
                }
              }}
              placeholder="e.g. Lagos Main Depot"
              className={fieldErrors.name ? errorInputClass : inputClass}
              required
            />
            {fieldErrors.name && (
              <p className="text-xs text-red-600 mt-1">{fieldErrors.name}</p>
            )}
          </div>

          {/* Fuel Type & Capacity */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="fuel-type"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Fuel Type
              </label>
              <select
                id="fuel-type"
                value={form.fuel_type}
                onChange={(e) =>
                  setForm({ ...form, fuel_type: e.target.value as FuelType })
                }
                className={inputClass}
              >
                {FUEL_TYPES.map((ft) => (
                  <option key={ft.value} value={ft.value}>
                    {ft.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label
                htmlFor="capacity-liters"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Capacity (Liters)
              </label>
              <input
                id="capacity-liters"
                type="number"
                value={form.capacity_liters || ""}
                onChange={(e) => {
                  const val = e.target.value === "" ? 0 : Number(e.target.value);
                  setForm({ ...form, capacity_liters: val });
                  if (fieldErrors.capacity_liters) {
                    setFieldErrors({
                      ...fieldErrors,
                      capacity_liters: undefined,
                    });
                  }
                }}
                placeholder="e.g. 50000"
                min="1"
                step="any"
                className={
                  fieldErrors.capacity_liters ? errorInputClass : inputClass
                }
                required
              />
              {fieldErrors.capacity_liters && (
                <p className="text-xs text-red-600 mt-1">
                  {fieldErrors.capacity_liters}
                </p>
              )}
            </div>
          </div>

          {/* Location Name */}
          <div>
            <label
              htmlFor="location-name"
              className="block text-xs font-medium text-gray-600 mb-1"
            >
              Location Name (optional)
            </label>
            <input
              id="location-name"
              type="text"
              value={form.location_name}
              onChange={(e) =>
                setForm({ ...form, location_name: e.target.value })
              }
              placeholder="e.g. Apapa Industrial Zone"
              className={inputClass}
            />
          </div>

          {/* Alert Threshold */}
          <div>
            <label
              htmlFor="alert-threshold"
              className="block text-xs font-medium text-gray-600 mb-1"
            >
              Alert Threshold (%)
            </label>
            <input
              id="alert-threshold"
              type="number"
              value={form.alert_threshold_pct}
              onChange={(e) => {
                const val =
                  e.target.value === "" ? 0 : Number(e.target.value);
                setForm({ ...form, alert_threshold_pct: val });
                if (fieldErrors.alert_threshold_pct) {
                  setFieldErrors({
                    ...fieldErrors,
                    alert_threshold_pct: undefined,
                  });
                }
              }}
              placeholder="e.g. 20"
              min="0"
              max="100"
              step="1"
              className={
                fieldErrors.alert_threshold_pct ? errorInputClass : inputClass
              }
              required
            />
            <p className="text-xs text-gray-400 mt-1">
              Alert when stock falls below this percentage of capacity
            </p>
            {fieldErrors.alert_threshold_pct && (
              <p className="text-xs text-red-600 mt-1">
                {fieldErrors.alert_threshold_pct}
              </p>
            )}
          </div>

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50"
              style={{ backgroundColor: "#232323" }}
            >
              {submitting ? submittingLabel : submitLabel}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
