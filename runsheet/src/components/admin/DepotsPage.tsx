"use client";

/**
 * Depot management (admin) page.
 *
 * Surfaces the tenant-configurable Depot entity introduced by the Fuel
 * Ops Hardening spec — Capability 2, Dynamic Dispatch & Replanning
 * (Requirements 2.2.1, 2.2.2, 2.2.7). Provides:
 *
 *  • Paginated list view of all depots for the tenant, wired to
 *    `GET /api/fuel/mvp/depots`.
 *  • Create / edit modal posting to `POST /api/fuel/mvp/depots` and
 *    `PATCH /api/fuel/mvp/depots/{depot_id}`. Inline delete with an
 *    undo-free confirmation prompt.
 *  • A prominent "depot_required" setup banner that renders whenever
 *    the tenant has zero depots OR no depot is flagged as default
 *    (Req 2.2.7 / design spec "tenants without a default depot SHALL
 *    be flagged in the admin UI with a 'depot_required' setup task").
 *  • A "Set as default" row action which PATCHes the target depot with
 *    `is_default: true`. The server may mirror that into the tenant
 *    config's `default_depot_id` — see
 *    {@link fuelApi.DepotUpdatePayload.is_default} for the follow-up.
 *
 * Styling mirrors the peer admin/ops pages under
 * `runsheet/src/components/ops/` (Tailwind utility classes, inline status
 * chips, toast system, `bg-black/30` modal overlays) so this page sits
 * visually alongside `CustomerTankPage` and `FuelDistributionPage`.
 *
 * Validates: Requirements 2.2.2, 2.2.7.
 */

import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock,
  Loader2,
  MapPin,
  Pencil,
  Plus,
  RefreshCw,
  Star,
  StarOff,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { type Column, Table } from "@/components/ui";
import {
  createDepot,
  type Depot,
  type DepotCreatePayload,
  type DepotListFilters,
  type DepotStatus,
  type DepotUpdatePayload,
  deleteDepot,
  listDepots,
  updateDepot,
} from "../../services/fuelApi";

// ─── Constants ───────────────────────────────────────────────────────────────

const PAGE_SIZE = 20;

const STATUSES: { value: DepotStatus; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "inactive", label: "Inactive" },
];

const STATUS_BADGE_CONFIG: Record<DepotStatus, { color: string; bg: string }> =
  {
    active: { color: "text-success-dark", bg: "bg-success-light" },
    inactive: { color: "text-gray-700", bg: "bg-gray-100" },
  };

// Canonical US fuel product codes the backend accepts directly without
// requiring alias resolution. Listed here so the create/edit form can
// offer a safe multi-select — the backend still canonicalizes freeform
// codes on write, but picking from this list avoids obvious typos.
const CANONICAL_PRODUCT_CODES: string[] = [
  "DIESEL_2",
  "OFF_ROAD_DIESEL",
  "HEATING_OIL",
  "GASOLINE_REG",
  "GASOLINE_PREM",
  "PROPANE",
  "KEROSENE",
  "DEF",
  "ETHANOL_E85",
];

// A conservative, commonly-used subset of IANA tz identifiers surfaced as
// a datalist. Users can still type any valid IANA name — the backend
// validates via `zoneinfo.ZoneInfo(...)` at write time.
const COMMON_TIMEZONES: string[] = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Phoenix",
  "America/Anchorage",
  "Pacific/Honolulu",
  "Europe/London",
  "Europe/Paris",
];

// Sentinel value used in filter <select> elements so "Any" maps to
// `undefined` without colliding with an actual status value.
const ANY_VALUE = "__any__";

// ─── Toast Notification System ───────────────────────────────────────────────

interface Toast {
  id: number;
  message: string;
  type: "success" | "error";
}

let toastIdCounter = 0;

function ToastContainer({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="fixed top-4 right-4 z-[100] space-y-2">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-sm font-medium ${
            toast.type === "success"
              ? "bg-success text-white"
              : "bg-error text-white"
          }`}
        >
          {toast.type === "success" ? (
            <Check className="w-4 h-4" />
          ) : (
            <AlertTriangle className="w-4 h-4" />
          )}
          <span>{toast.message}</span>
          <button
            type="button"
            onClick={() => onDismiss(toast.id)}
            className="ml-2 p-0.5 hover:bg-white/20 rounded"
            aria-label="Dismiss notification"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      ))}
    </div>
  );
}

function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((message: string, type: "success" | "error") => {
    const id = ++toastIdCounter;
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return { toasts, addToast, dismissToast };
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * A depot is the tenant default when its record's `is_default` flag is set.
 * The backend :class:`fuel.depot_models.Depot` model round-trips `is_default`
 * on every read (list and single-depot reads), and the `Depot` type now
 * declares the field, so this reads it directly rather than inferring the
 * default from a loosely-typed shape (cross-module-entity-linkage Req 10.3).
 */
export function isDefaultDepot(depot: Depot): boolean {
  return depot.is_default === true;
}

function formatCoordinates(depot: Depot): string {
  return `${depot.location_lat.toFixed(5)}, ${depot.location_lon.toFixed(5)}`;
}

// ─── Status Badge ────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: DepotStatus }) {
  const config = STATUS_BADGE_CONFIG[status] ?? STATUS_BADGE_CONFIG.inactive;
  return (
    <span
      className={`inline-flex items-center text-[10px] px-2 py-0.5 rounded font-medium ${config.bg} ${config.color}`}
    >
      {status}
    </span>
  );
}

// ─── Setup Banner ────────────────────────────────────────────────────────────

interface SetupBannerProps {
  reason: "empty" | "no_default";
  onCreate: () => void;
}

/**
 * "depot_required" setup banner shown at the top of the page whenever
 * the tenant either has no depots at all or has depots but none flagged
 * as default. Renders a primary CTA that opens the create modal so the
 * setup task is resolvable in one click.
 *
 * Validates: Requirement 2.2.7.
 */
function SetupBanner({ reason, onCreate }: SetupBannerProps) {
  const copy =
    reason === "empty"
      ? {
          title: "Depot setup required",
          body: "This tenant has no depots configured yet. The route solver cannot generate plans until at least one depot exists — create one below to unblock dispatching.",
          cta: "Create first depot",
        }
      : {
          title: "Default depot required",
          body: "No depot is flagged as the tenant default. Route planning falls back to the default depot when a truck has no assigned_depot_id, so pick one before running the solver.",
          cta: "Create another depot",
        };

  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-xl border border-warning-light bg-warning-light px-4 py-3"
      data-testid="depot-required-banner"
      data-reason={reason}
    >
      <div className="mt-0.5 shrink-0 rounded-full bg-warning-light p-1.5">
        <AlertTriangle
          className="h-4 w-4 text-warning-dark"
          aria-hidden="true"
        />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold text-warning-dark">{copy.title}</p>
        <p className="mt-1 text-xs text-warning-dark">{copy.body}</p>
      </div>
      <button
        type="button"
        onClick={onCreate}
        className="inline-flex items-center gap-1.5 shrink-0 px-3 py-2 text-xs font-medium text-white rounded-lg bg-primary hover:bg-primary-hover"
      >
        <Plus className="w-3.5 h-3.5" aria-hidden="true" />
        {copy.cta}
      </button>
    </div>
  );
}

// ─── Filters Row ─────────────────────────────────────────────────────────────

export interface DepotFilters {
  status?: DepotStatus;
  fuel_type?: string;
}

interface FiltersRowProps {
  filters: DepotFilters;
  onChange: (next: DepotFilters) => void;
  onReset: () => void;
  loading?: boolean;
  onRefresh?: () => void;
}

function FiltersRow({
  filters,
  onChange,
  onReset,
  loading,
  onRefresh,
}: FiltersRowProps) {
  const selectClass =
    "px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="flex flex-wrap items-end gap-3">
      <div>
        <label
          htmlFor="dp-filter-status"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          Status
        </label>
        <select
          id="dp-filter-status"
          className={selectClass}
          value={filters.status ?? ANY_VALUE}
          onChange={(e) =>
            onChange({
              ...filters,
              status:
                e.target.value === ANY_VALUE
                  ? undefined
                  : (e.target.value as DepotStatus),
            })
          }
        >
          <option value={ANY_VALUE}>Any</option>
          {STATUSES.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label
          htmlFor="dp-filter-fuel-type"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          Supports Fuel
        </label>
        <select
          id="dp-filter-fuel-type"
          className={selectClass}
          value={filters.fuel_type ?? ANY_VALUE}
          onChange={(e) =>
            onChange({
              ...filters,
              fuel_type:
                e.target.value === ANY_VALUE ? undefined : e.target.value,
            })
          }
        >
          <option value={ANY_VALUE}>Any</option>
          {CANONICAL_PRODUCT_CODES.map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onReset}
          className="px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50 border border-gray-200"
        >
          Reset
        </button>
        {onRefresh && (
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            className="inline-flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50 border border-gray-200 disabled:opacity-50"
            aria-label="Refresh depots"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`}
              aria-hidden="true"
            />
            Refresh
          </button>
        )}
      </div>
    </div>
  );
}

// ─── Form Validation ─────────────────────────────────────────────────────────

export interface DepotFormValues {
  depot_id: string;
  name: string;
  location_lat: number;
  location_lon: number;
  address: string;
  timezone: string;
  fuel_types_supported: string[];
  status: DepotStatus;
}

export interface DepotFormErrors {
  name?: string;
  location_lat?: string;
  location_lon?: string;
  address?: string;
  timezone?: string;
  fuel_types_supported?: string;
}

/**
 * Pure validation for the create/edit form. Mirrors backend
 * :class:`Depot` field-level constraints so the UI catches obvious
 * mistakes before the round-trip.
 *
 * Validates: Requirement 2.2.1 (coordinate ranges, required name / address /
 * timezone, ≥1 fuel_type_supported per the task's validation spec).
 */
export function validateDepotForm(values: DepotFormValues): DepotFormErrors {
  const errors: DepotFormErrors = {};

  if (!values.name || !values.name.trim()) {
    errors.name = "Depot name is required.";
  }
  if (
    values.location_lat == null ||
    Number.isNaN(values.location_lat) ||
    values.location_lat < -90 ||
    values.location_lat > 90
  ) {
    errors.location_lat = "Latitude must be between -90 and 90.";
  }
  if (
    values.location_lon == null ||
    Number.isNaN(values.location_lon) ||
    values.location_lon < -180 ||
    values.location_lon > 180
  ) {
    errors.location_lon = "Longitude must be between -180 and 180.";
  }
  if (!values.address || !values.address.trim()) {
    errors.address = "Address is required.";
  }
  if (!values.timezone || !values.timezone.trim()) {
    errors.timezone =
      "Timezone is required (IANA format, e.g. America/Chicago).";
  }
  if (
    !values.fuel_types_supported ||
    values.fuel_types_supported.length === 0
  ) {
    errors.fuel_types_supported = "Select at least one supported fuel product.";
  }

  return errors;
}

// ─── Create / Edit Modal ─────────────────────────────────────────────────────

interface DepotFormModalProps {
  mode: "create" | "edit";
  depot?: Depot | null;
  onClose: () => void;
  onSuccess: (depot: Depot, mode: "create" | "edit") => void;
}

function DepotFormModal({
  mode,
  depot,
  onClose,
  onSuccess,
}: DepotFormModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [apiError, setApiError] = useState("");
  const [fieldErrors, setFieldErrors] = useState<DepotFormErrors>({});

  const [form, setForm] = useState<DepotFormValues>(() => ({
    depot_id: depot?.depot_id ?? "",
    name: depot?.name ?? "",
    location_lat: depot?.location_lat ?? 0,
    location_lon: depot?.location_lon ?? 0,
    address: depot?.address ?? "",
    timezone: depot?.timezone ?? "America/Chicago",
    fuel_types_supported: depot?.fuel_types_supported
      ? [...depot.fuel_types_supported]
      : [],
    status: depot?.status ?? "active",
  }));

  const title = mode === "create" ? "Add Depot" : "Edit Depot";
  const submitLabel = mode === "create" ? "Create Depot" : "Save Changes";
  const submittingLabel = mode === "create" ? "Creating..." : "Saving...";

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";
  const errorInputClass =
    "w-full px-3 py-2 text-sm border border-error rounded-lg focus:ring-2 focus:ring-error-light focus:border-error bg-white";

  function updateField<K extends keyof DepotFormValues>(
    key: K,
    value: DepotFormValues[K],
  ) {
    setForm((prev) => ({ ...prev, [key]: value }));
    if (key in fieldErrors) {
      setFieldErrors((prev) => ({ ...prev, [key]: undefined }));
    }
  }

  function toggleFuelType(code: string) {
    setForm((prev) => {
      const next = prev.fuel_types_supported.includes(code)
        ? prev.fuel_types_supported.filter((c) => c !== code)
        : [...prev.fuel_types_supported, code];
      return { ...prev, fuel_types_supported: next };
    });
    setFieldErrors((prev) => ({ ...prev, fuel_types_supported: undefined }));
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    const errors = validateDepotForm(form);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    setApiError("");
    setSubmitting(true);

    try {
      let result: Depot;
      if (mode === "create") {
        const payload: DepotCreatePayload = {
          name: form.name.trim(),
          location_lat: form.location_lat,
          location_lon: form.location_lon,
          address: form.address.trim(),
          timezone: form.timezone.trim(),
          fuel_types_supported: form.fuel_types_supported,
          status: form.status,
        };
        if (form.depot_id.trim()) {
          payload.depot_id = form.depot_id.trim();
        }
        result = await createDepot(payload);
      } else {
        if (!depot) throw new Error("Missing depot reference for edit.");
        const patch: DepotUpdatePayload = {};
        if (form.name.trim() !== depot.name) patch.name = form.name.trim();
        if (form.location_lat !== depot.location_lat)
          patch.location_lat = form.location_lat;
        if (form.location_lon !== depot.location_lon)
          patch.location_lon = form.location_lon;
        if (form.address.trim() !== depot.address)
          patch.address = form.address.trim();
        if (form.timezone.trim() !== depot.timezone)
          patch.timezone = form.timezone.trim();
        if (
          !arraysShallowEqual(
            form.fuel_types_supported,
            depot.fuel_types_supported,
          )
        ) {
          patch.fuel_types_supported = form.fuel_types_supported;
        }
        if (form.status !== depot.status) patch.status = form.status;

        if (Object.keys(patch).length === 0) {
          // Nothing changed — close without touching the server.
          onClose();
          return;
        }

        result = await updateDepot(depot.depot_id, patch);
      }
      onSuccess(result, mode);
      onClose();
    } catch (err) {
      setApiError(err instanceof Error ? err.message : "Failed to save depot.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-primary">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close depot form"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {apiError && (
            <p className="text-sm text-error bg-error-light px-3 py-2 rounded-lg">
              {apiError}
            </p>
          )}

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="dp-name"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Name
              </label>
              <input
                id="dp-name"
                type="text"
                className={fieldErrors.name ? errorInputClass : inputClass}
                value={form.name}
                onChange={(e) => updateField("name", e.target.value)}
                placeholder="e.g. Chicago Main Yard"
                required
              />
              {fieldErrors.name && (
                <p className="text-xs text-error mt-1">{fieldErrors.name}</p>
              )}
            </div>

            <div>
              <label
                htmlFor="dp-depot-id"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Depot ID {mode === "create" && "(optional)"}
              </label>
              <input
                id="dp-depot-id"
                type="text"
                className={inputClass}
                value={form.depot_id}
                onChange={(e) => updateField("depot_id", e.target.value)}
                placeholder={
                  mode === "create"
                    ? "Auto-generated if blank"
                    : "Immutable once set"
                }
                disabled={mode === "edit"}
              />
            </div>
          </div>

          <div>
            <label
              htmlFor="dp-address"
              className="block text-xs font-medium text-gray-600 mb-1"
            >
              Address
            </label>
            <input
              id="dp-address"
              type="text"
              className={fieldErrors.address ? errorInputClass : inputClass}
              value={form.address}
              onChange={(e) => updateField("address", e.target.value)}
              placeholder="e.g. 1000 N Halsted St, Chicago, IL 60642"
              required
            />
            {fieldErrors.address && (
              <p className="text-xs text-error mt-1">{fieldErrors.address}</p>
            )}
          </div>

          <div className="grid grid-cols-3 gap-4">
            <div>
              <label
                htmlFor="dp-lat"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Latitude
              </label>
              <input
                id="dp-lat"
                type="number"
                step="any"
                min="-90"
                max="90"
                className={
                  fieldErrors.location_lat ? errorInputClass : inputClass
                }
                value={form.location_lat || ""}
                onChange={(e) =>
                  updateField(
                    "location_lat",
                    e.target.value === "" ? 0 : Number(e.target.value),
                  )
                }
                placeholder="e.g. 41.8781"
                required
              />
              {fieldErrors.location_lat && (
                <p className="text-xs text-error mt-1">
                  {fieldErrors.location_lat}
                </p>
              )}
            </div>

            <div>
              <label
                htmlFor="dp-lon"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Longitude
              </label>
              <input
                id="dp-lon"
                type="number"
                step="any"
                min="-180"
                max="180"
                className={
                  fieldErrors.location_lon ? errorInputClass : inputClass
                }
                value={form.location_lon || ""}
                onChange={(e) =>
                  updateField(
                    "location_lon",
                    e.target.value === "" ? 0 : Number(e.target.value),
                  )
                }
                placeholder="e.g. -87.6298"
                required
              />
              {fieldErrors.location_lon && (
                <p className="text-xs text-error mt-1">
                  {fieldErrors.location_lon}
                </p>
              )}
            </div>

            <div>
              <label
                htmlFor="dp-timezone"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Timezone (IANA)
              </label>
              <input
                id="dp-timezone"
                type="text"
                list="dp-timezone-options"
                className={fieldErrors.timezone ? errorInputClass : inputClass}
                value={form.timezone}
                onChange={(e) => updateField("timezone", e.target.value)}
                placeholder="e.g. America/Chicago"
                required
              />
              <datalist id="dp-timezone-options">
                {COMMON_TIMEZONES.map((tz) => (
                  <option key={tz} value={tz} />
                ))}
              </datalist>
              {fieldErrors.timezone && (
                <p className="text-xs text-error mt-1">
                  {fieldErrors.timezone}
                </p>
              )}
            </div>
          </div>

          <div>
            <fieldset>
              <legend className="block text-xs font-medium text-gray-600 mb-1">
                Supported Fuel Products
              </legend>
              <div className="flex flex-wrap gap-2">
                {CANONICAL_PRODUCT_CODES.map((code) => {
                  const selected = form.fuel_types_supported.includes(code);
                  return (
                    <button
                      key={code}
                      type="button"
                      onClick={() => toggleFuelType(code)}
                      className={`px-2.5 py-1 text-xs rounded-md border transition-colors ${
                        selected
                          ? "bg-primary text-white border-primary"
                          : "bg-white text-gray-700 border-gray-200 hover:bg-gray-50"
                      }`}
                      aria-pressed={selected}
                    >
                      {code}
                    </button>
                  );
                })}
              </div>
              {fieldErrors.fuel_types_supported && (
                <p className="text-xs text-error mt-1">
                  {fieldErrors.fuel_types_supported}
                </p>
              )}
              <p className="text-xs text-gray-400 mt-1">
                Pick every canonical product this depot can load.
              </p>
            </fieldset>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="dp-status"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Status
              </label>
              <select
                id="dp-status"
                className={inputClass}
                value={form.status}
                onChange={(e) =>
                  updateField("status", e.target.value as DepotStatus)
                }
              >
                {STATUSES.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

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
              className="px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50 bg-primary hover:bg-primary-hover"
            >
              {submitting ? submittingLabel : submitLabel}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Delete Confirmation Modal ───────────────────────────────────────────────

interface DeleteConfirmModalProps {
  depot: Depot;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}

function DeleteConfirmModal({
  depot,
  onCancel,
  onConfirm,
}: DeleteConfirmModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const handleConfirm = async () => {
    setError("");
    setSubmitting(true);
    try {
      await onConfirm();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete depot.");
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-primary">Delete depot?</h2>
          <button
            type="button"
            onClick={onCancel}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close delete confirmation"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="px-6 py-4 space-y-3">
          <p className="text-sm text-gray-700">
            This will permanently remove{" "}
            <span className="font-medium text-primary">{depot.name}</span> (
            {depot.depot_id}). Trucks currently assigned to this depot will fall
            back to the tenant default on the next plan.
          </p>
          {error && (
            <p className="text-sm text-error bg-error-light px-3 py-2 rounded-lg">
              {error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-3 px-6 pb-4">
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={submitting}
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50 bg-error hover:bg-error-dark"
          >
            <Trash2 className="w-3.5 h-3.5" aria-hidden="true" />
            {submitting ? "Deleting..." : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Main Page ───────────────────────────────────────────────────────────────

export interface DepotsPageProps {
  /** Initial filter state; useful when linking in from another page. */
  initialFilters?: DepotFilters;
}

export default function DepotsPage({ initialFilters }: DepotsPageProps = {}) {
  const { toasts, addToast, dismissToast } = useToasts();

  const [filters, setFilters] = useState<DepotFilters>(initialFilters ?? {});
  const [page, setPage] = useState(1);
  const [depots, setDepots] = useState<Depot[]>([]);
  const [totalWindow, setTotalWindow] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [formOpen, setFormOpen] = useState<null | {
    mode: "create" | "edit";
    depot?: Depot;
  }>(null);
  const [deleteTarget, setDeleteTarget] = useState<Depot | null>(null);
  const [settingDefaultId, setSettingDefaultId] = useState<string | null>(null);

  const loadDepots = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError("");
      try {
        const query: DepotListFilters = {
          ...filters,
          page,
          size: PAGE_SIZE,
        };
        const response = await listDepots(query);
        if (signal?.aborted) return;
        setDepots(response.items);
        setTotalWindow(response.total);
        setHasNext(response.has_next);
      } catch (err) {
        if (signal?.aborted) return;
        setError(err instanceof Error ? err.message : "Failed to load depots.");
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [filters, page],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadDepots(controller.signal);
    return () => controller.abort();
  }, [loadDepots]);

  const setFiltersAndReset = useCallback((next: DepotFilters) => {
    setFilters(next);
    setPage(1);
  }, []);

  const onResetFilters = useCallback(() => {
    setFilters({});
    setPage(1);
  }, []);

  const handleCreateSuccess = useCallback(
    (depot: Depot, mode: "create" | "edit") => {
      addToast(
        mode === "create"
          ? `Created depot ${depot.depot_id}.`
          : `Updated depot ${depot.depot_id}.`,
        "success",
      );
      loadDepots();
    },
    [addToast, loadDepots],
  );

  const handleDelete = useCallback(
    async (depot: Depot) => {
      await deleteDepot(depot.depot_id);
      addToast(`Deleted depot ${depot.depot_id}.`, "success");
      setDeleteTarget(null);
      loadDepots();
    },
    [addToast, loadDepots],
  );

  const handleSetDefault = useCallback(
    async (depot: Depot) => {
      setSettingDefaultId(depot.depot_id);
      try {
        // NOTE: See fuelApi.DepotUpdatePayload.is_default for the
        // follow-up. If the backend hasn't wired is_default through to
        // tenant_settings.default_depot_id yet, this PATCH will either
        // 422 (extra="forbid") or succeed without mirroring — the UI
        // surfaces the error inline and leaves the list unchanged.
        await updateDepot(depot.depot_id, { is_default: true });
        addToast(`Set ${depot.name} as tenant default.`, "success");
        loadDepots();
      } catch (err) {
        addToast(
          err instanceof Error
            ? `Set-as-default failed: ${err.message}`
            : "Set-as-default failed.",
          "error",
        );
      } finally {
        setSettingDefaultId(null);
      }
    },
    [addToast, loadDepots],
  );

  // "depot_required" banner trigger: zero depots OR no default flagged.
  // Only surface the banner when we're on the first page and the current
  // filters are cleared, otherwise "no results" from a narrow filter
  // would spuriously trigger setup warnings.
  const bannerReason: "empty" | "no_default" | null = useMemo(() => {
    const filtersActive = !!filters.status || !!filters.fuel_type || page !== 1;
    if (filtersActive) return null;
    if (!loading && depots.length === 0 && totalWindow === 0) {
      return "empty";
    }
    if (depots.length > 0 && !depots.some(isDefaultDepot)) {
      return "no_default";
    }
    return null;
  }, [filters, page, loading, depots, totalWindow]);

  const paginationSummary = useMemo(() => {
    if (depots.length === 0) return "No depots on this page";
    const start = (page - 1) * PAGE_SIZE + 1;
    const end = start + depots.length - 1;
    return `Showing ${start}–${end} of ${totalWindow}${hasNext ? "+" : ""}`;
  }, [depots.length, page, totalWindow, hasNext]);

  const depotColumns: Column<Depot>[] = [
    {
      key: "depot",
      label: "Depot",
      className: "text-sm",
      render: (depot) => {
        const isDefault = isDefaultDepot(depot);
        return (
          <>
            <div className="flex items-center gap-2">
              <span className="font-medium text-primary">{depot.name}</span>
              {isDefault && (
                <span
                  title="Tenant default depot"
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-warning-light text-warning-dark"
                >
                  <Star className="w-2.5 h-2.5" aria-hidden="true" />
                  default
                </span>
              )}
            </div>
            <div className="text-xs text-gray-500">{depot.depot_id}</div>
          </>
        );
      },
    },
    {
      key: "location",
      label: "Location",
      className: "text-sm text-gray-700",
      render: (depot) => (
        <div className="flex items-start gap-1">
          <MapPin
            className="w-3 h-3 text-gray-400 mt-0.5 shrink-0"
            aria-hidden="true"
          />
          <div>
            <div className="line-clamp-2">{depot.address}</div>
            <div className="text-xs text-gray-400 font-mono">
              {formatCoordinates(depot)}
            </div>
          </div>
        </div>
      ),
    },
    {
      key: "timezone",
      label: "Timezone",
      className: "text-sm text-gray-700",
      render: (depot) => (
        <span className="inline-flex items-center gap-1">
          <Clock className="w-3 h-3 text-gray-400" aria-hidden="true" />
          {depot.timezone}
        </span>
      ),
    },
    {
      key: "products",
      label: "Products",
      render: (depot) => (
        <div className="flex flex-wrap gap-1 max-w-[240px]">
          {depot.fuel_types_supported.length === 0 ? (
            <span className="text-xs text-gray-400">—</span>
          ) : (
            depot.fuel_types_supported.map((code) => (
              <span
                key={code}
                className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-info-light text-info-dark"
              >
                {code}
              </span>
            ))
          )}
        </div>
      ),
    },
    {
      key: "status",
      label: "Status",
      render: (depot) => <StatusBadge status={depot.status} />,
    },
    {
      key: "actions",
      label: "Actions",
      align: "right",
      render: (depot) => {
        const isDefault = isDefaultDepot(depot);
        return (
          <div className="inline-flex items-center gap-1">
            <button
              type="button"
              onClick={() => handleSetDefault(depot)}
              disabled={isDefault || settingDefaultId === depot.depot_id}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-gray-600 bg-gray-100 rounded-md hover:bg-gray-200 hover:text-gray-800 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              aria-label={
                isDefault
                  ? `${depot.name} is the tenant default`
                  : `Set ${depot.name} as tenant default`
              }
              title={
                isDefault
                  ? "Already the tenant default"
                  : "Set as tenant default"
              }
            >
              {settingDefaultId === depot.depot_id ? (
                <Loader2 className="w-3 h-3 animate-spin" aria-hidden="true" />
              ) : isDefault ? (
                <Star className="w-3 h-3" aria-hidden="true" />
              ) : (
                <StarOff className="w-3 h-3" aria-hidden="true" />
              )}
              {isDefault ? "Default" : "Set default"}
            </button>
            <button
              type="button"
              onClick={() => setFormOpen({ mode: "edit", depot })}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-gray-600 bg-gray-100 rounded-md hover:bg-gray-200 hover:text-gray-800 transition-colors"
              aria-label={`Edit depot ${depot.depot_id}`}
            >
              <Pencil className="w-3 h-3" aria-hidden="true" />
              Edit
            </button>
            <button
              type="button"
              onClick={() => setDeleteTarget(depot)}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-error bg-error-light rounded-md hover:bg-error-light hover:text-error-dark transition-colors"
              aria-label={`Delete depot ${depot.depot_id}`}
            >
              <Trash2 className="w-3 h-3" aria-hidden="true" />
              Delete
            </button>
          </div>
        );
      },
    },
  ];

  return (
    <div className="min-h-screen bg-gray-50">
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />

      <div className="max-w-7xl mx-auto px-6 py-6 space-y-6">
        {/* Header */}
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-xl font-semibold text-primary">Depots</h1>
            <p className="text-sm text-gray-500 mt-1">
              Loading yards trucks start and end at. Required by the route
              solver — the agent falls back to the tenant default when a truck
              has no assigned_depot_id.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setFormOpen({ mode: "create" })}
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50 bg-primary hover:bg-primary-hover"
          >
            <Plus className="w-4 h-4" aria-hidden="true" />
            Add Depot
          </button>
        </div>

        {/* Setup banner (Req 2.2.7) */}
        {bannerReason && (
          <SetupBanner
            reason={bannerReason}
            onCreate={() => setFormOpen({ mode: "create" })}
          />
        )}

        {/* Filters */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
          <FiltersRow
            filters={filters}
            onChange={setFiltersAndReset}
            onReset={onResetFilters}
            loading={loading}
            onRefresh={() => loadDepots()}
          />
        </div>

        {/* Error banner */}
        {error && (
          <div className="bg-error-light border border-error-light rounded-lg px-4 py-3 flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-error" aria-hidden="true" />
            <p className="text-sm text-error-dark">{error}</p>
          </div>
        )}

        {/* List */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
          <Table<Depot>
            ariaLabel="Depot list"
            columns={depotColumns}
            data={depots}
            getRowId={(depot) => depot.depot_id}
            loading={loading && depots.length === 0}
            loadingState={
              <div className="flex items-center justify-center py-4">
                <Loader2 className="w-5 h-5 text-gray-400 animate-spin" />
              </div>
            }
            emptyState={
              <div className="text-gray-500">
                <p className="text-lg font-medium text-gray-400">
                  No depots found
                </p>
                <p className="text-sm text-gray-400 mt-1">
                  Try adjusting your filters or add a new depot.
                </p>
              </div>
            }
          />

          {/* Pagination */}
          {depots.length > 0 && (
            <div className="flex items-center justify-between px-6 py-3 border-t border-gray-100 bg-gray-50">
              <p className="text-xs text-gray-500">{paginationSummary}</p>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1 || loading}
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-xs text-gray-600 rounded-md hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed"
                  aria-label="Previous page"
                >
                  <ChevronLeft className="w-3 h-3" aria-hidden="true" />
                  Prev
                </button>
                <span className="text-xs text-gray-500 tabular-nums">
                  Page {page}
                </span>
                <button
                  type="button"
                  onClick={() => setPage((p) => p + 1)}
                  disabled={!hasNext || loading}
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-xs text-gray-600 rounded-md hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed"
                  aria-label="Next page"
                >
                  Next
                  <ChevronRight className="w-3 h-3" aria-hidden="true" />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      {formOpen && (
        <DepotFormModal
          mode={formOpen.mode}
          depot={formOpen.depot}
          onClose={() => setFormOpen(null)}
          onSuccess={handleCreateSuccess}
        />
      )}

      {deleteTarget && (
        <DeleteConfirmModal
          depot={deleteTarget}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => handleDelete(deleteTarget)}
        />
      )}
    </div>
  );
}

// ─── Internal Utilities ──────────────────────────────────────────────────────

function arraysShallowEqual(a: string[], b: string[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  const sortedA = [...a].sort();
  const sortedB = [...b].sort();
  for (let i = 0; i < sortedA.length; i++) {
    if (sortedA[i] !== sortedB[i]) return false;
  }
  return true;
}
