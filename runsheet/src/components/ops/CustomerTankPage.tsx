"use client";

/**
 * Customer Tank management page.
 *
 * Surfaces the per-customer fuel tanks introduced by the Fuel Ops Hardening
 * spec (Capability 1 — Tank Forecasting) as a standalone ops page. Provides:
 *
 *  • Paginated list view of all customer tanks for the tenant, wired to
 *    `GET /api/fuel/mvp/customer-tanks`.
 *  • Create / edit modal posting to `POST /api/fuel/mvp/customer-tanks`
 *    and `PATCH /api/fuel/mvp/customer-tanks/{id}`.
 *  • Filter controls for customer_type, fuel_type, and zip_code —
 *    forwarded as query-string params the backend already supports.
 *  • A k_factor indicator column (null-safe) so propane dispatchers can
 *    spot which tanks still need enough history to compute a per-tank
 *    consumption coefficient.
 *
 * Styling mirrors existing pages under `runsheet/src/components/ops/`
 * (Tailwind utility classes + inline status chips + `bg-black/30` modal
 * overlays) so this page sits visually alongside `FuelDistributionPage`
 * and `FuelStationList`.
 *
 * Validates: Requirements 1.1.4, 1.6.2, 1.6.3.
 */

import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
  Gauge,
  MapPin,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { type Column, EntityLink, Table } from "@/components/ui";
import { type Customer, getCustomers } from "../../services/commerceApi";
import type {
  CustomerTank,
  CustomerTankCreatePayload,
  CustomerTankCustomerType,
  CustomerTankForecast,
  CustomerTankFuelType,
  CustomerTankStatus,
  CustomerTankUpdatePayload,
  CustomerTankUseCase,
  FuelProductItem,
} from "../../services/fuelApi";
import {
  createCustomerTank,
  listCustomerTankForecasts,
  listCustomerTanks,
  listFuelProducts,
  updateCustomerTank,
} from "../../services/fuelApi";
import { getCurrentTenantId } from "../../services/tenant";

// ─── Constants ───────────────────────────────────────────────────────────────

const PAGE_SIZE = 20;

const CUSTOMER_TYPES: { value: CustomerTankCustomerType; label: string }[] = [
  { value: "residential", label: "Residential" },
  { value: "commercial", label: "Commercial" },
  { value: "keep_full", label: "Keep Full" },
  { value: "will_call", label: "Will Call" },
  { value: "auto_fill", label: "Auto Fill" },
];

const FUEL_TYPES: { value: CustomerTankFuelType; label: string }[] = [
  { value: "propane", label: "Propane" },
  { value: "heating_oil", label: "Heating Oil" },
  { value: "diesel", label: "Diesel" },
  { value: "generator_fuel", label: "Generator Fuel" },
  { value: "farm_fuel", label: "Farm Fuel" },
  { value: "gasoline", label: "Gasoline" },
];

// Default canonical product codes per fuel family — the backend accepts
// aliases, but posting a canonical code from the start avoids 422s on write.
const DEFAULT_PRODUCT_CODE: Record<CustomerTankFuelType, string> = {
  propane: "PROPANE",
  heating_oil: "HEATING_OIL",
  diesel: "DIESEL_2",
  generator_fuel: "DIESEL_2",
  farm_fuel: "OFF_ROAD_DIESEL",
  gasoline: "GASOLINE_REG",
};

const STATUSES: { value: CustomerTankStatus; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "inactive", label: "Inactive" },
  { value: "maintenance", label: "Maintenance" },
];

const USE_CASES: { value: CustomerTankUseCase; label: string }[] = [
  { value: "residential_heat", label: "Residential Heat" },
  { value: "commercial_heat", label: "Commercial Heat" },
  { value: "generator", label: "Generator" },
  { value: "farm", label: "Farm" },
  { value: "other", label: "Other" },
];

const STATUS_BADGE_CONFIG: Record<
  CustomerTankStatus,
  { color: string; bg: string }
> = {
  active: { color: "text-success-dark", bg: "bg-success-light" },
  inactive: { color: "text-gray-700", bg: "bg-gray-100" },
  maintenance: { color: "text-warning-dark", bg: "bg-warning-light" },
};

// Sentinel values used in filter <select> elements so "Any" can map to
// `undefined` without colliding with empty strings from typed inputs.
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

function formatGallons(gallons: number | null | undefined): string {
  if (gallons == null || Number.isNaN(gallons)) return "—";
  if (gallons >= 1_000) return `${(gallons / 1_000).toFixed(1)}K`;
  return gallons.toFixed(0);
}

function levelPct(tank: CustomerTank): number {
  if (!tank.capacity_gallons || tank.capacity_gallons <= 0) return 0;
  return Math.min(
    100,
    (tank.current_level_gallons / tank.capacity_gallons) * 100,
  );
}

function formatKFactor(k: number | null | undefined): {
  label: string;
  color: string;
  bg: string;
  title: string;
} {
  if (k == null || Number.isNaN(k)) {
    return {
      label: "n/a",
      color: "text-gray-500",
      bg: "bg-gray-100",
      title: "K-factor not yet computed (needs ≥ 3 delivery intervals).",
    };
  }
  const label = k.toFixed(2);
  if (k >= 0.6) {
    return {
      label,
      color: "text-info-dark",
      bg: "bg-info-light",
      title:
        "High consumption coefficient — likely commercial or heavy-use tank.",
    };
  }
  if (k >= 0.3) {
    return {
      label,
      color: "text-success-dark",
      bg: "bg-success-light",
      title: "Typical residential K-factor range.",
    };
  }
  return {
    label,
    color: "text-warning-dark",
    bg: "bg-warning-light",
    title: "Low K-factor — tank may be under-used or seasonal.",
  };
}

/**
 * Describe a tank's runout forecast for the dispatcher. Uses the
 * conservative ``hours_to_runout_p90`` (or p50 fallback) to label how
 * soon a delivery is needed, colour-coded by urgency. This is the
 * forecast-driven signal unique to Customer Tanks (vs. the static
 * days-until-empty monitoring view on Fuel Stations).
 */
export function formatRunoutForecast(forecast: CustomerTankForecast | null): {
  label: string;
  detail: string;
  color: string;
  bg: string;
  title: string;
} {
  if (!forecast) {
    return {
      label: "No forecast",
      detail: "—",
      color: "text-gray-400",
      bg: "bg-gray-100",
      title:
        "No runout forecast yet. The forecaster needs consumption history and an active tank.",
    };
  }

  const hours = forecast.hours_to_runout_p90 ?? forecast.hours_to_runout_p50;
  const risk = forecast.runout_risk_24h;

  if (hours == null) {
    return {
      label: "No forecast",
      detail: risk != null ? `${Math.round(risk * 100)}% risk / 24h` : "—",
      color: "text-gray-400",
      bg: "bg-gray-100",
      title: "Forecast exists but did not produce an hours-to-runout estimate.",
    };
  }

  const days = hours / 24;
  const detail =
    hours < 48 ? `~${Math.round(hours)}h` : `~${days.toFixed(1)} days`;
  const riskPct = risk != null ? `${Math.round(risk * 100)}% / 24h` : "";

  // Urgency thresholds mirror the dispatch-priority buckets: a tank
  // forecast to run dry within a day is critical, within two days high.
  if (hours <= 24 || (risk != null && risk >= 0.7)) {
    return {
      label: "Critical",
      detail: riskPct ? `${detail} · ${riskPct}` : detail,
      color: "text-error-dark",
      bg: "bg-error-light",
      title:
        "Forecast to run dry within ~24h (or ≥70% 24-hour runout risk). Schedule a delivery now.",
    };
  }
  if (hours <= 48) {
    return {
      label: "Soon",
      detail: riskPct ? `${detail} · ${riskPct}` : detail,
      color: "text-warning-dark",
      bg: "bg-warning-light",
      title: "Forecast to run dry within ~2 days. Queue a delivery.",
    };
  }
  return {
    label: "OK",
    detail,
    color: "text-success-dark",
    bg: "bg-success-light",
    title: "Healthy runway before the next delivery is needed.",
  };
}

// ─── Status Badge ────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: CustomerTankStatus }) {
  const config = STATUS_BADGE_CONFIG[status] ?? STATUS_BADGE_CONFIG.inactive;
  return (
    <span
      className={`inline-flex items-center text-[10px] px-2 py-0.5 rounded font-medium ${config.bg} ${config.color}`}
    >
      {status}
    </span>
  );
}

// ─── Filters Row ─────────────────────────────────────────────────────────────

export interface CustomerTankFilters {
  customer_type?: CustomerTankCustomerType;
  fuel_type?: CustomerTankFuelType;
  zip_code?: string;
  status?: CustomerTankStatus;
}

interface FiltersRowProps {
  filters: CustomerTankFilters;
  onChange: (next: CustomerTankFilters) => void;
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
  const inputClass =
    "pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="flex flex-wrap items-end gap-3">
      <div>
        <label
          htmlFor="ct-filter-customer-type"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          Customer Type
        </label>
        <select
          id="ct-filter-customer-type"
          className={selectClass}
          value={filters.customer_type ?? ANY_VALUE}
          onChange={(e) =>
            onChange({
              ...filters,
              customer_type:
                e.target.value === ANY_VALUE
                  ? undefined
                  : (e.target.value as CustomerTankCustomerType),
            })
          }
        >
          <option value={ANY_VALUE}>Any</option>
          {CUSTOMER_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label
          htmlFor="ct-filter-fuel-type"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          Fuel Type
        </label>
        <select
          id="ct-filter-fuel-type"
          className={selectClass}
          value={filters.fuel_type ?? ANY_VALUE}
          onChange={(e) =>
            onChange({
              ...filters,
              fuel_type:
                e.target.value === ANY_VALUE
                  ? undefined
                  : (e.target.value as CustomerTankFuelType),
            })
          }
        >
          <option value={ANY_VALUE}>Any</option>
          {FUEL_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label
          htmlFor="ct-filter-status"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          Status
        </label>
        <select
          id="ct-filter-status"
          className={selectClass}
          value={filters.status ?? ANY_VALUE}
          onChange={(e) =>
            onChange({
              ...filters,
              status:
                e.target.value === ANY_VALUE
                  ? undefined
                  : (e.target.value as CustomerTankStatus),
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
          htmlFor="ct-filter-zip"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          ZIP Code
        </label>
        <div className="relative">
          <Search
            className="w-3.5 h-3.5 text-gray-400 absolute left-2.5 top-1/2 -translate-y-1/2"
            aria-hidden="true"
          />
          <input
            id="ct-filter-zip"
            type="text"
            placeholder="e.g. 10001"
            inputMode="numeric"
            className={inputClass}
            value={filters.zip_code ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                zip_code: e.target.value.trim() || undefined,
              })
            }
          />
        </div>
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
            aria-label="Refresh customer tanks"
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

export interface CustomerTankFormValues {
  customer_tank_id: string;
  customer_id: string;
  last_refill_order_id: string;
  customer_type: CustomerTankCustomerType;
  fuel_type: CustomerTankFuelType;
  fuel_product_code: string;
  capacity_gallons: number;
  current_level_gallons: number;
  location_lat: number;
  location_lon: number;
  zip_code: string;
  k_factor: number | null;
  use_case: CustomerTankUseCase | "";
  status: CustomerTankStatus;
}

export interface CustomerTankFormErrors {
  customer_id?: string;
  fuel_product_code?: string;
  capacity_gallons?: string;
  current_level_gallons?: string;
  location_lat?: string;
  location_lon?: string;
  zip_code?: string;
  k_factor?: string;
}

/**
 * Pure validation for the create/edit form. Mirrors backend
 * :class:`CustomerTank` field-level constraints so the UI catches obvious
 * mistakes before the round-trip.
 */
export function validateCustomerTankForm(
  values: CustomerTankFormValues,
): CustomerTankFormErrors {
  const errors: CustomerTankFormErrors = {};

  if (!values.customer_id || !values.customer_id.trim()) {
    errors.customer_id = "Customer ID is required.";
  }
  if (!values.fuel_product_code || !values.fuel_product_code.trim()) {
    errors.fuel_product_code = "Fuel product code is required.";
  }
  if (
    values.capacity_gallons == null ||
    Number.isNaN(values.capacity_gallons) ||
    values.capacity_gallons <= 0
  ) {
    errors.capacity_gallons = "Capacity must be greater than zero.";
  }
  if (
    values.current_level_gallons == null ||
    Number.isNaN(values.current_level_gallons) ||
    values.current_level_gallons < 0
  ) {
    errors.current_level_gallons = "Current level must be zero or greater.";
  } else if (
    values.capacity_gallons > 0 &&
    values.current_level_gallons > values.capacity_gallons
  ) {
    errors.current_level_gallons = "Current level cannot exceed capacity.";
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
  if (!values.zip_code || !values.zip_code.trim()) {
    errors.zip_code = "ZIP code is required.";
  }
  if (
    values.k_factor != null &&
    (Number.isNaN(values.k_factor) || values.k_factor < 0)
  ) {
    errors.k_factor = "K-factor must be zero or greater.";
  }

  return errors;
}

// ─── Create / Edit Modal ─────────────────────────────────────────────────────

interface CustomerTankFormModalProps {
  mode: "create" | "edit";
  tank?: CustomerTank | null;
  onClose: () => void;
  onSuccess: (tank: CustomerTank, mode: "create" | "edit") => void;
}

function CustomerTankFormModal({
  mode,
  tank,
  onClose,
  onSuccess,
}: CustomerTankFormModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [apiError, setApiError] = useState("");
  const [fieldErrors, setFieldErrors] = useState<CustomerTankFormErrors>({});
  // Fuel product catalog for the canonical-code combobox (Req 6.1.3).
  // ``null`` means "still loading"; an empty array means "loaded with no
  // catalog rows" (the backend returns 200 + empty list for tenants
  // whose Region has no configured products — we fall back to the
  // free-text input in that case too).
  const [fuelProducts, setFuelProducts] = useState<FuelProductItem[] | null>(
    null,
  );
  // When the catalog fetch fails we silently fall back to the legacy
  // free-text input. Logged via ``console.error`` per the spec.
  const [fuelProductsFailed, setFuelProductsFailed] = useState(false);

  // Commerce customers for the validated customer picker (cross-module-entity-
  // linkage Req 7.1/7.3): the customer_id field is no longer free-text — the
  // dispatcher selects from the tenant's canonical commerce customers so the
  // backend write-time reference validation always passes. ``null`` means the
  // list is still loading; a failed fetch falls back to a free-text input so a
  // commerce outage never blocks tank creation.
  const [customers, setCustomers] = useState<Customer[] | null>(null);
  const [customersFailed, setCustomersFailed] = useState(false);

  const [form, setForm] = useState<CustomerTankFormValues>(() => ({
    customer_tank_id: tank?.customer_tank_id ?? "",
    customer_id: tank?.customer_id ?? "",
    last_refill_order_id: tank?.last_refill_order_id ?? "",
    customer_type: tank?.customer_type ?? "residential",
    fuel_type: tank?.fuel_type ?? "propane",
    fuel_product_code: tank?.fuel_product_code ?? DEFAULT_PRODUCT_CODE.propane,
    capacity_gallons: tank?.capacity_gallons ?? 500,
    current_level_gallons: tank?.current_level_gallons ?? 0,
    location_lat: tank?.location_lat ?? 0,
    location_lon: tank?.location_lon ?? 0,
    zip_code: tank?.zip_code ?? "",
    k_factor: tank?.k_factor ?? null,
    use_case: (tank?.use_case as CustomerTankUseCase | undefined) ?? "",
    status: tank?.status ?? "active",
  }));

  const title = mode === "create" ? "Add Customer Tank" : "Edit Customer Tank";
  const submitLabel = mode === "create" ? "Create Tank" : "Save Changes";
  const submittingLabel = mode === "create" ? "Creating..." : "Saving...";

  // Fetch the tenant's fuel product catalog once per modal open so the
  // fuel_product_code input can surface canonical codes as dropdown
  // suggestions (Req 6.1.3). A failed fetch silently falls back to the
  // legacy free-text input; submission is never blocked on this.
  //
  // Note: there is no dedicated Jest test file for CustomerTankPage as
  // of Phase 2 Batch D. The Playwright smoke run covers the render
  // path end-to-end. When a ``CustomerTankPage.test.tsx`` is added,
  // assert that the datalist rendered by this effect contains the
  // canonical product codes returned by :func:`listFuelProducts`.
  useEffect(() => {
    let cancelled = false;
    listFuelProducts()
      .then((res) => {
        if (!cancelled) setFuelProducts(res.items);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error("Failed to load fuel product catalog", err);
        setFuelProductsFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Load commerce customers once per modal open for the validated picker
  // (Req 7.1). A failed fetch falls back to free-text entry.
  useEffect(() => {
    let cancelled = false;
    getCustomers({ limit: 200 })
      .then((res) => {
        if (!cancelled) setCustomers(res.data ?? []);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error("Failed to load commerce customers", err);
        setCustomersFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";
  const errorInputClass =
    "w-full px-3 py-2 text-sm border border-error rounded-lg focus:ring-2 focus:ring-error-light focus:border-error bg-white";

  function updateField<K extends keyof CustomerTankFormValues>(
    key: K,
    value: CustomerTankFormValues[K],
  ) {
    setForm((prev) => ({ ...prev, [key]: value }));
    if (key in fieldErrors) {
      setFieldErrors((prev) => ({ ...prev, [key]: undefined }));
    }
  }

  function onFuelTypeChange(next: CustomerTankFuelType) {
    setForm((prev) => ({
      ...prev,
      fuel_type: next,
      // Only auto-populate product_code when the user hasn't customized it.
      fuel_product_code:
        prev.fuel_product_code === DEFAULT_PRODUCT_CODE[prev.fuel_type] ||
        !prev.fuel_product_code
          ? DEFAULT_PRODUCT_CODE[next]
          : prev.fuel_product_code,
    }));
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    const errors = validateCustomerTankForm(form);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    setApiError("");
    setSubmitting(true);

    try {
      let result: CustomerTank;
      if (mode === "create") {
        const payload: CustomerTankCreatePayload = {
          customer_id: form.customer_id.trim(),
          customer_type: form.customer_type,
          fuel_type: form.fuel_type,
          fuel_product_code: form.fuel_product_code.trim(),
          capacity_gallons: form.capacity_gallons,
          current_level_gallons: form.current_level_gallons,
          location_lat: form.location_lat,
          location_lon: form.location_lon,
          zip_code: form.zip_code.trim(),
          status: form.status,
        };
        if (form.customer_tank_id.trim()) {
          payload.customer_tank_id = form.customer_tank_id.trim();
        }
        if (form.last_refill_order_id.trim()) {
          payload.last_refill_order_id = form.last_refill_order_id.trim();
        }
        if (form.k_factor != null) payload.k_factor = form.k_factor;
        if (form.use_case) payload.use_case = form.use_case;
        result = await createCustomerTank(payload);
      } else {
        if (!tank) throw new Error("Missing tank reference for edit.");
        const patch: CustomerTankUpdatePayload = {};
        if (form.customer_id.trim() !== tank.customer_id)
          patch.customer_id = form.customer_id.trim();
        if (form.customer_type !== tank.customer_type)
          patch.customer_type = form.customer_type;
        if (form.fuel_type !== tank.fuel_type) patch.fuel_type = form.fuel_type;
        if (form.fuel_product_code.trim() !== tank.fuel_product_code)
          patch.fuel_product_code = form.fuel_product_code.trim();
        if (form.capacity_gallons !== tank.capacity_gallons)
          patch.capacity_gallons = form.capacity_gallons;
        if (form.current_level_gallons !== tank.current_level_gallons)
          patch.current_level_gallons = form.current_level_gallons;
        if (form.location_lat !== tank.location_lat)
          patch.location_lat = form.location_lat;
        if (form.location_lon !== tank.location_lon)
          patch.location_lon = form.location_lon;
        if (form.zip_code.trim() !== tank.zip_code)
          patch.zip_code = form.zip_code.trim();
        if (
          form.last_refill_order_id.trim() &&
          form.last_refill_order_id.trim() !== (tank.last_refill_order_id ?? "")
        )
          patch.last_refill_order_id = form.last_refill_order_id.trim();
        if ((form.k_factor ?? null) !== (tank.k_factor ?? null))
          patch.k_factor = form.k_factor;
        const currentUseCase = tank.use_case ?? "";
        if (form.use_case !== currentUseCase) {
          patch.use_case = form.use_case
            ? (form.use_case as CustomerTankUseCase)
            : undefined;
        }
        if (form.status !== tank.status) patch.status = form.status;

        result = await updateCustomerTank(tank.customer_tank_id, patch);
      }
      onSuccess(result, mode);
      onClose();
    } catch (err) {
      setApiError(
        err instanceof Error ? err.message : "Failed to save customer tank.",
      );
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
            aria-label="Close customer tank form"
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
                htmlFor="ct-customer-id"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Customer ID
              </label>
              {customers !== null && !customersFailed ? (
                // Validated picker (cross-module-entity-linkage Req 7.1): the
                // dispatcher selects a canonical commerce customer rather than
                // typing a free-text id, so the backend write-time reference
                // validation always resolves. In edit mode we surface the
                // tank's current customer_id even when it falls outside the
                // loaded page so the selection is never silently lost.
                <select
                  id="ct-customer-id"
                  className={
                    fieldErrors.customer_id ? errorInputClass : inputClass
                  }
                  value={form.customer_id}
                  onChange={(e) => updateField("customer_id", e.target.value)}
                  required
                >
                  <option value="">Select a customer…</option>
                  {form.customer_id &&
                    !customers.some(
                      (c) => c.customer_id === form.customer_id,
                    ) && (
                      <option value={form.customer_id}>
                        {form.customer_id} (current)
                      </option>
                    )}
                  {customers.map((c) => (
                    <option key={c.customer_id} value={c.customer_id}>
                      {c.display_name} ({c.customer_id})
                    </option>
                  ))}
                </select>
              ) : (
                // Fallback: a commerce-customer fetch failed (or is still
                // loading) — degrade to free-text so a commerce outage never
                // blocks tank management. The backend still validates the ref.
                <input
                  id="ct-customer-id"
                  type="text"
                  className={
                    fieldErrors.customer_id ? errorInputClass : inputClass
                  }
                  value={form.customer_id}
                  onChange={(e) => updateField("customer_id", e.target.value)}
                  placeholder="e.g. CUST-0042"
                  required
                />
              )}
              {fieldErrors.customer_id && (
                <p className="text-xs text-error mt-1">
                  {fieldErrors.customer_id}
                </p>
              )}
            </div>

            <div>
              <label
                htmlFor="ct-tank-id"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Tank ID {mode === "create" && "(optional)"}
              </label>
              <input
                id="ct-tank-id"
                type="text"
                className={inputClass}
                value={form.customer_tank_id}
                onChange={(e) =>
                  updateField("customer_tank_id", e.target.value)
                }
                placeholder={
                  mode === "create"
                    ? "Auto-generated if blank"
                    : "Immutable once set"
                }
                disabled={mode === "edit"}
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="ct-refill-order"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Refilling Order ID (optional)
              </label>
              <input
                id="ct-refill-order"
                type="text"
                className={inputClass}
                value={form.last_refill_order_id}
                onChange={(e) =>
                  updateField("last_refill_order_id", e.target.value)
                }
                placeholder="e.g. ORD-0042"
              />
              <p className="text-xs text-gray-400 mt-1">
                The delivery order that most recently refilled this tank.
              </p>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="ct-customer-type"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Customer Type
              </label>
              <select
                id="ct-customer-type"
                className={inputClass}
                value={form.customer_type}
                onChange={(e) =>
                  updateField(
                    "customer_type",
                    e.target.value as CustomerTankCustomerType,
                  )
                }
              >
                {CUSTOMER_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label
                htmlFor="ct-fuel-type"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Fuel Type
              </label>
              <select
                id="ct-fuel-type"
                className={inputClass}
                value={form.fuel_type}
                onChange={(e) =>
                  onFuelTypeChange(e.target.value as CustomerTankFuelType)
                }
              >
                {FUEL_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="ct-product-code"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Fuel Product Code
              </label>
              <input
                id="ct-product-code"
                type="text"
                className={
                  fieldErrors.fuel_product_code ? errorInputClass : inputClass
                }
                value={form.fuel_product_code}
                onChange={(e) =>
                  updateField("fuel_product_code", e.target.value.toUpperCase())
                }
                placeholder="e.g. PROPANE"
                required
                list={
                  !fuelProductsFailed && fuelProducts && fuelProducts.length > 0
                    ? "ct-product-code-options"
                    : undefined
                }
                autoComplete="off"
              />
              {!fuelProductsFailed &&
                fuelProducts &&
                fuelProducts.length > 0 && (
                  <datalist id="ct-product-code-options">
                    {fuelProducts.map((p) => (
                      <option
                        key={p.product_code}
                        value={p.product_code}
                        label={p.display_name}
                      >
                        {p.display_name}
                      </option>
                    ))}
                  </datalist>
                )}
              {fieldErrors.fuel_product_code && (
                <p className="text-xs text-error mt-1">
                  {fieldErrors.fuel_product_code}
                </p>
              )}
            </div>

            <div>
              <label
                htmlFor="ct-use-case"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Use Case (optional)
              </label>
              <select
                id="ct-use-case"
                className={inputClass}
                value={form.use_case}
                onChange={(e) =>
                  updateField(
                    "use_case",
                    e.target.value as CustomerTankUseCase | "",
                  )
                }
              >
                <option value="">—</option>
                {USE_CASES.map((u) => (
                  <option key={u.value} value={u.value}>
                    {u.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="ct-capacity"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Capacity (gallons)
              </label>
              <input
                id="ct-capacity"
                type="number"
                min="1"
                step="any"
                className={
                  fieldErrors.capacity_gallons ? errorInputClass : inputClass
                }
                value={form.capacity_gallons || ""}
                onChange={(e) =>
                  updateField(
                    "capacity_gallons",
                    e.target.value === "" ? 0 : Number(e.target.value),
                  )
                }
                placeholder="e.g. 500"
                required
              />
              {fieldErrors.capacity_gallons && (
                <p className="text-xs text-error mt-1">
                  {fieldErrors.capacity_gallons}
                </p>
              )}
            </div>

            <div>
              <label
                htmlFor="ct-level"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Current Level (gallons)
              </label>
              <input
                id="ct-level"
                type="number"
                min="0"
                step="any"
                className={
                  fieldErrors.current_level_gallons
                    ? errorInputClass
                    : inputClass
                }
                value={
                  form.current_level_gallons === 0
                    ? form.current_level_gallons
                    : form.current_level_gallons || ""
                }
                onChange={(e) =>
                  updateField(
                    "current_level_gallons",
                    e.target.value === "" ? 0 : Number(e.target.value),
                  )
                }
                placeholder="e.g. 180"
                required
              />
              {fieldErrors.current_level_gallons && (
                <p className="text-xs text-error mt-1">
                  {fieldErrors.current_level_gallons}
                </p>
              )}
            </div>
          </div>

          <div className="grid grid-cols-3 gap-4">
            <div>
              <label
                htmlFor="ct-lat"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Latitude
              </label>
              <input
                id="ct-lat"
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
                placeholder="e.g. 40.7128"
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
                htmlFor="ct-lon"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Longitude
              </label>
              <input
                id="ct-lon"
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
                placeholder="e.g. -74.0060"
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
                htmlFor="ct-zip"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                ZIP Code
              </label>
              <input
                id="ct-zip"
                type="text"
                inputMode="numeric"
                className={fieldErrors.zip_code ? errorInputClass : inputClass}
                value={form.zip_code}
                onChange={(e) => updateField("zip_code", e.target.value)}
                placeholder="e.g. 10001"
                required
              />
              {fieldErrors.zip_code && (
                <p className="text-xs text-error mt-1">
                  {fieldErrors.zip_code}
                </p>
              )}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="ct-kfactor"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                K-Factor (optional)
              </label>
              <input
                id="ct-kfactor"
                type="number"
                step="0.01"
                min="0"
                className={fieldErrors.k_factor ? errorInputClass : inputClass}
                value={form.k_factor ?? ""}
                onChange={(e) =>
                  updateField(
                    "k_factor",
                    e.target.value === "" ? null : Number(e.target.value),
                  )
                }
                placeholder="Auto-computed after 3+ deliveries"
              />
              {fieldErrors.k_factor && (
                <p className="text-xs text-error mt-1">
                  {fieldErrors.k_factor}
                </p>
              )}
              <p className="text-xs text-gray-400 mt-1">
                Gallons consumed per Heating Degree Day. Leave blank to let the
                forecaster learn from delivery history.
              </p>
            </div>

            <div>
              <label
                htmlFor="ct-status"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Status
              </label>
              <select
                id="ct-status"
                className={inputClass}
                value={form.status}
                onChange={(e) =>
                  updateField("status", e.target.value as CustomerTankStatus)
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

// ─── Main Page ───────────────────────────────────────────────────────────────

export interface CustomerTankPageProps {
  /** Initial filter state; useful when linking in from another page. */
  initialFilters?: CustomerTankFilters;
}

export default function CustomerTankPage({
  initialFilters,
}: CustomerTankPageProps = {}) {
  const { toasts, addToast, dismissToast } = useToasts();

  const [filters, setFilters] = useState<CustomerTankFilters>(
    initialFilters ?? {},
  );
  const [page, setPage] = useState(1);
  const [tanks, setTanks] = useState<CustomerTank[]>([]);
  const [forecastsByTank, setForecastsByTank] = useState<
    Record<string, CustomerTankForecast>
  >({});
  const [totalWindow, setTotalWindow] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [formOpen, setFormOpen] = useState<null | {
    mode: "create" | "edit";
    tank?: CustomerTank;
  }>(null);

  const loadTanks = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError("");
      try {
        const response = await listCustomerTanks({
          ...filters,
          page,
          size: PAGE_SIZE,
        });
        if (signal?.aborted) return;
        setTanks(response.items);
        setTotalWindow(response.total);
        setHasNext(response.has_next);

        // Join each tank to its latest runout forecast. The forecasts index
        // mixes retail-station forecasts (customer_type "retail") with
        // customer-tank forecasts; filtering by the distinct customer_types
        // present on this page returns only the customer-tank forecasts and
        // keeps the fetch bounded (1–3 small requests) regardless of how many
        // station forecasts exist. Failures here are non-fatal: the tank list
        // still renders, the forecast column just shows "No forecast".
        try {
          const tenantId = getCurrentTenantId();
          const types = Array.from(
            new Set(response.items.map((t) => t.customer_type)),
          );
          const results = await Promise.all(
            types.map((customer_type) =>
              listCustomerTankForecasts({
                tenant_id: tenantId,
                customer_type,
                size: 100,
              }).catch(() => null),
            ),
          );
          if (signal?.aborted) return;
          const map: Record<string, CustomerTankForecast> = {};
          for (const res of results) {
            if (!res) continue;
            for (const f of res.data) {
              const id = f.customer_tank_id;
              // Backend sorts by timestamp desc, so the first row seen per
              // customer_tank_id is the freshest.
              if (id && !(id in map)) map[id] = f;
            }
          }
          setForecastsByTank(map);
        } catch {
          if (!signal?.aborted) setForecastsByTank({});
        }
      } catch (err) {
        if (signal?.aborted) return;
        setError(
          err instanceof Error ? err.message : "Failed to load customer tanks.",
        );
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [filters, page],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadTanks(controller.signal);
    return () => controller.abort();
  }, [loadTanks]);

  // Reset to page 1 whenever a filter changes so paging stays sensible.
  const setFiltersAndReset = useCallback((next: CustomerTankFilters) => {
    setFilters(next);
    setPage(1);
  }, []);

  const onResetFilters = useCallback(() => {
    setFilters({});
    setPage(1);
  }, []);

  const handleCreateSuccess = useCallback(
    (tank: CustomerTank, mode: "create" | "edit") => {
      addToast(
        mode === "create"
          ? `Created tank ${tank.customer_tank_id}.`
          : `Updated tank ${tank.customer_tank_id}.`,
        "success",
      );
      // Reload so the new/updated record reflects backend-side product
      // canonicalization on fuel_product_code.
      loadTanks();
    },
    [addToast, loadTanks],
  );

  const paginationSummary = useMemo(() => {
    if (tanks.length === 0) return "No tanks on this page";
    const start = (page - 1) * PAGE_SIZE + 1;
    const end = start + tanks.length - 1;
    return `Showing ${start}–${end} of ${totalWindow}${hasNext ? "+" : ""}`;
  }, [tanks.length, page, totalWindow, hasNext]);

  const tankColumns: Column<CustomerTank>[] = [
    {
      key: "tank_customer",
      label: "Tank / Customer",
      render: (tank) => (
        <div className="text-sm">
          <div className="font-medium text-primary">
            {tank.customer_tank_id}
          </div>
          <div className="text-xs">
            {/* Navigable link to the owning commerce customer (Req 7.3, 13.1).
                List reads carry no resolver `links`, so link optimistically on
                the raw customer_id. */}
            <EntityLink
              type="customer"
              id={tank.customer_id}
              showId={false}
              stopPropagation
            />
          </div>
        </div>
      ),
    },
    {
      key: "last_refill_order",
      label: "Refilling Order",
      render: (tank) => (
        // The order whose delivery most recently refilled this tank (Req 7.2).
        // Renders a navigable link, or a neutral placeholder when unset.
        <div className="text-sm">
          <EntityLink
            type="order"
            id={tank.last_refill_order_id ?? null}
            showId={false}
            stopPropagation
          />
        </div>
      ),
    },
    {
      key: "customer_type",
      label: "Type",
      className: "text-sm text-gray-700 capitalize",
      render: (tank) => tank.customer_type.replace(/_/g, " "),
    },
    {
      key: "fuel",
      label: "Fuel",
      className: "text-sm text-gray-700",
      render: (tank) => (
        <>
          <div className="capitalize">{tank.fuel_type.replace(/_/g, " ")}</div>
          <div className="text-xs text-gray-400">{tank.fuel_product_code}</div>
        </>
      ),
    },
    {
      key: "capacity_level",
      label: "Capacity / Level",
      render: (tank) => {
        const pct = levelPct(tank);
        return (
          <>
            <div className="flex items-center gap-2">
              <div
                className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden min-w-[80px]"
                role="progressbar"
                aria-valuenow={Math.round(pct)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`Fill level ${Math.round(pct)}%`}
              >
                <div
                  className="h-full rounded-full bg-info"
                  style={{ width: `${pct}%` }}
                />
              </div>
              <span className="text-xs text-gray-600 w-14 text-right">
                {pct.toFixed(0)}%
              </span>
            </div>
            <div className="text-xs text-gray-400 mt-0.5">
              {formatGallons(tank.current_level_gallons)} /{" "}
              {formatGallons(tank.capacity_gallons)} gal
            </div>
          </>
        );
      },
    },
    {
      key: "zip_code",
      label: "ZIP",
      className: "text-sm text-gray-700",
      render: (tank) => (
        <span className="inline-flex items-center gap-1">
          <MapPin className="w-3 h-3 text-gray-400" aria-hidden="true" />
          {tank.zip_code}
        </span>
      ),
    },
    {
      key: "k_factor",
      label: "K-Factor",
      render: (tank) => {
        const k = formatKFactor(tank.k_factor);
        return (
          <span
            title={k.title}
            className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${k.bg} ${k.color}`}
          >
            <Gauge className="w-3 h-3" aria-hidden="true" />
            {k.label}
          </span>
        );
      },
    },
    {
      key: "runout_forecast",
      label: "Runout Forecast",
      render: (tank) => {
        const fc = formatRunoutForecast(
          forecastsByTank[tank.customer_tank_id] ?? null,
        );
        return (
          <>
            <span
              title={fc.title}
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${fc.bg} ${fc.color}`}
            >
              {fc.label}
            </span>
            <div className="text-xs text-gray-400 mt-0.5">{fc.detail}</div>
          </>
        );
      },
    },
    {
      key: "status",
      label: "Status",
      render: (tank) => <StatusBadge status={tank.status} />,
    },
    {
      key: "actions",
      label: "Actions",
      align: "right",
      render: (tank) => (
        <button
          type="button"
          onClick={() => setFormOpen({ mode: "edit", tank })}
          className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-gray-600 bg-gray-100 rounded-md hover:bg-gray-200 hover:text-gray-800 transition-colors"
          aria-label={`Edit tank ${tank.customer_tank_id}`}
        >
          <Pencil className="w-3 h-3" aria-hidden="true" />
          Edit
        </button>
      ),
    },
  ];

  return (
    <div className="min-h-screen bg-gray-50">
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />

      <div className="max-w-7xl mx-auto px-6 py-6 space-y-6">
        {/* Header */}
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-xl font-semibold text-primary">
              Customer Tanks
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Per-customer fuel tanks used by the forecaster to drive runout
              predictions, K-factor learning, and storm-mode prioritization.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setFormOpen({ mode: "create" })}
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50 bg-primary hover:bg-primary-hover"
          >
            <Plus className="w-4 h-4" aria-hidden="true" />
            Add Tank
          </button>
        </div>

        {/* Filters */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
          <FiltersRow
            filters={filters}
            onChange={setFiltersAndReset}
            onReset={onResetFilters}
            loading={loading}
            onRefresh={() => loadTanks()}
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
          <Table<CustomerTank>
            ariaLabel="Customer tank list"
            columns={tankColumns}
            data={tanks}
            getRowId={(tank) => tank.customer_tank_id}
            loading={loading && tanks.length === 0}
            emptyState={
              <div className="text-gray-500">
                <p className="text-lg font-medium text-gray-400">
                  No customer tanks found
                </p>
                <p className="text-sm text-gray-400 mt-1">
                  Try adjusting your filters or add a new tank.
                </p>
              </div>
            }
          />

          {/* Pagination */}
          {tanks.length > 0 && (
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
        <CustomerTankFormModal
          mode={formOpen.mode}
          tank={formOpen.tank}
          onClose={() => setFormOpen(null)}
          onSuccess={handleCreateSuccess}
        />
      )}
    </div>
  );
}
