"use client";

/**
 * Create Order Modal — dispatcher keyboard intake form.
 *
 * Posts to `POST /api/orders`. Populates `intake_metadata.dispatcher_user_id`
 * from the JWT-derived user context. Validates client-side using the same
 * rules as the backend (`missing_volume`, `invalid_delivery_window`,
 * `will_call` allows null window).
 *
 * Validates: Requirements 2.4, 8.1.4
 */

import { Loader2, X } from "lucide-react";
import { useCallback, useState } from "react";
import { ApiError } from "../../services/api";
import {
  type CallType,
  type CreateOrderPayload,
  createOrder,
} from "../../services/ordersApi";
import CustomerPicker from "./CustomerPicker";
import ProductPicker from "./ProductPicker";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface CreateOrderFormValues {
  customer_id: string;
  customer_name: string;
  customer_phone: string;
  customer_email: string;
  ship_to_address: string;
  ship_to_lat: string;
  ship_to_lon: string;
  customer_tank_id: string;
  product_code: string;
  gallons_requested: string;
  fill_to_full: boolean;
  call_type: CallType;
  delivery_window_start: string;
  delivery_window_end: string;
  po_number: string;
  special_instructions: string;
}

export interface CreateOrderFormErrors {
  customer_id?: string;
  customer_name?: string;
  ship_to_address?: string;
  ship_to_lat?: string;
  ship_to_lon?: string;
  product_code?: string;
  gallons_requested?: string;
  delivery_window_start?: string;
  delivery_window_end?: string;
  general?: string;
}

export interface CreateOrderModalProps {
  /** Whether the modal is open */
  isOpen: boolean;
  /** Close the modal */
  onClose: () => void;
  /** Called after successful order creation */
  onSuccess?: (orderId: string) => void;
  /** Dispatcher user ID from JWT context */
  dispatcherUserId?: string;
}

// ─── Validation ──────────────────────────────────────────────────────────────

/**
 * Client-side validation matching backend rules:
 * - `missing_volume`: gallons_requested must be > 0 unless fill_to_full
 * - `invalid_delivery_window`: end must be after start when both present;
 *   window is required for `one_off` call_type
 * - `will_call` / `keep_full` / `auto_fill` allow null window
 */
export function validateCreateOrderForm(
  values: CreateOrderFormValues,
): CreateOrderFormErrors {
  const errors: CreateOrderFormErrors = {};

  if (!values.customer_id.trim()) {
    errors.customer_id = "Customer ID is required";
  }
  if (!values.customer_name.trim()) {
    errors.customer_name = "Customer name is required";
  }
  if (!values.ship_to_address.trim()) {
    errors.ship_to_address = "Ship-to address is required";
  }

  const lat = parseFloat(values.ship_to_lat);
  if (Number.isNaN(lat) || lat < -90 || lat > 90) {
    errors.ship_to_lat = "Latitude must be between -90 and 90";
  }

  const lon = parseFloat(values.ship_to_lon);
  if (Number.isNaN(lon) || lon < -180 || lon > 180) {
    errors.ship_to_lon = "Longitude must be between -180 and 180";
  }

  if (!values.product_code.trim()) {
    errors.product_code = "Product code is required";
  }

  // Volume validation: missing_volume
  if (!values.fill_to_full) {
    const gallons = parseFloat(values.gallons_requested);
    if (
      !values.gallons_requested.trim() ||
      Number.isNaN(gallons) ||
      gallons <= 0
    ) {
      errors.gallons_requested =
        "Gallons must be greater than 0 (or select Fill to Full)";
    }
  }

  // Delivery window validation: invalid_delivery_window
  if (values.call_type === "one_off") {
    if (!values.delivery_window_start) {
      errors.delivery_window_start =
        "Delivery window start is required for one-off orders";
    }
    if (!values.delivery_window_end) {
      errors.delivery_window_end =
        "Delivery window end is required for one-off orders";
    }
  }

  if (values.delivery_window_start && values.delivery_window_end) {
    const start = new Date(values.delivery_window_start);
    const end = new Date(values.delivery_window_end);
    if (end <= start) {
      errors.delivery_window_end = "Delivery window end must be after start";
    }
  }

  return errors;
}

// ─── Initial form state ──────────────────────────────────────────────────────

const INITIAL_FORM: CreateOrderFormValues = {
  customer_id: "",
  customer_name: "",
  customer_phone: "",
  customer_email: "",
  ship_to_address: "",
  ship_to_lat: "",
  ship_to_lon: "",
  customer_tank_id: "",
  product_code: "",
  gallons_requested: "",
  fill_to_full: false,
  call_type: "one_off",
  delivery_window_start: "",
  delivery_window_end: "",
  po_number: "",
  special_instructions: "",
};

// ─── Component ───────────────────────────────────────────────────────────────

export default function CreateOrderModal({
  isOpen,
  onClose,
  onSuccess,
  dispatcherUserId,
}: CreateOrderModalProps) {
  const [form, setForm] = useState<CreateOrderFormValues>(INITIAL_FORM);
  const [errors, setErrors] = useState<CreateOrderFormErrors>({});
  const [submitting, setSubmitting] = useState(false);

  const handleChange = useCallback(
    (field: keyof CreateOrderFormValues, value: string | boolean) => {
      setForm((prev) => ({ ...prev, [field]: value }));
      // Clear field error on change
      setErrors((prev) => ({
        ...prev,
        [field]: undefined,
        general: undefined,
      }));
    },
    [],
  );

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();

      const validationErrors = validateCreateOrderForm(form);
      if (Object.keys(validationErrors).length > 0) {
        setErrors(validationErrors);
        return;
      }

      setSubmitting(true);
      setErrors({});

      try {
        const payload: CreateOrderPayload = {
          customer_id: form.customer_id.trim(),
          customer_name: form.customer_name.trim(),
          customer_phone: form.customer_phone.trim() || undefined,
          customer_email: form.customer_email.trim() || undefined,
          ship_to_address: form.ship_to_address.trim(),
          ship_to_lat: parseFloat(form.ship_to_lat),
          ship_to_lon: parseFloat(form.ship_to_lon),
          customer_tank_id: form.customer_tank_id.trim() || undefined,
          product_code: form.product_code.trim(),
          gallons_requested: form.fill_to_full
            ? undefined
            : parseFloat(form.gallons_requested),
          fill_to_full: form.fill_to_full,
          call_type: form.call_type,
          delivery_window_start: form.delivery_window_start || undefined,
          delivery_window_end: form.delivery_window_end || undefined,
          po_number: form.po_number.trim() || undefined,
          special_instructions: form.special_instructions.trim() || undefined,
          client_event_id:
            typeof crypto !== "undefined" && crypto.randomUUID
              ? crypto.randomUUID()
              : `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        };

        const response = await createOrder(payload);
        setForm(INITIAL_FORM);
        onSuccess?.(response.data.order_id);
        onClose();
      } catch (err) {
        if (err instanceof ApiError) {
          setErrors({ general: err.message });
        } else {
          setErrors({
            general:
              err instanceof Error ? err.message : "Failed to create order",
          });
        }
      } finally {
        setSubmitting(false);
      }
    },
    [form, onClose, onSuccess, dispatcherUserId],
  );

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      role="dialog"
      aria-modal="true"
      aria-labelledby="create-order-title"
    >
      <div className="bg-white rounded-xl shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto mx-4">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2
            id="create-order-title"
            className="text-lg font-semibold text-primary"
          >
            Create Order
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors"
            aria-label="Close modal"
          >
            <X className="w-5 h-5 text-gray-500" />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {errors.general && (
            <div
              role="alert"
              className="rounded-lg bg-error-light border border-error-light px-4 py-3 text-sm text-error-dark"
            >
              {errors.general}
            </div>
          )}

          {/* Customer section */}
          <fieldset className="space-y-3">
            <legend className="text-sm font-medium text-gray-700">
              Customer
            </legend>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label
                  htmlFor="co-customer-id"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Customer ID *
                </label>
                <CustomerPicker
                  id="co-customer-id"
                  aria-label="Customer ID"
                  value={form.customer_id || null}
                  onChange={(value) => handleChange("customer_id", value)}
                  allowClear
                />
                {errors.customer_id && (
                  <p className="text-xs text-error mt-1">
                    {errors.customer_id}
                  </p>
                )}
              </div>
              <div>
                <label
                  htmlFor="co-customer-name"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Customer Name *
                </label>
                <input
                  id="co-customer-name"
                  type="text"
                  value={form.customer_name}
                  onChange={(e) =>
                    handleChange("customer_name", e.target.value)
                  }
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                />
                {errors.customer_name && (
                  <p className="text-xs text-error mt-1">
                    {errors.customer_name}
                  </p>
                )}
              </div>
              <div>
                <label
                  htmlFor="co-phone"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Phone
                </label>
                <input
                  id="co-phone"
                  type="tel"
                  value={form.customer_phone}
                  onChange={(e) =>
                    handleChange("customer_phone", e.target.value)
                  }
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                />
              </div>
              <div>
                <label
                  htmlFor="co-email"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Email
                </label>
                <input
                  id="co-email"
                  type="email"
                  value={form.customer_email}
                  onChange={(e) =>
                    handleChange("customer_email", e.target.value)
                  }
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                />
              </div>
            </div>
          </fieldset>

          {/* Delivery location */}
          <fieldset className="space-y-3">
            <legend className="text-sm font-medium text-gray-700">
              Delivery Location
            </legend>
            <div>
              <label
                htmlFor="co-address"
                className="block text-xs text-gray-600 mb-1"
              >
                Ship-to Address *
              </label>
              <input
                id="co-address"
                type="text"
                value={form.ship_to_address}
                onChange={(e) =>
                  handleChange("ship_to_address", e.target.value)
                }
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
              />
              {errors.ship_to_address && (
                <p className="text-xs text-error mt-1">
                  {errors.ship_to_address}
                </p>
              )}
            </div>
            <div className="grid grid-cols-3 gap-3">
              <div>
                <label
                  htmlFor="co-lat"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Latitude *
                </label>
                <input
                  id="co-lat"
                  type="number"
                  step="any"
                  value={form.ship_to_lat}
                  onChange={(e) => handleChange("ship_to_lat", e.target.value)}
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                />
                {errors.ship_to_lat && (
                  <p className="text-xs text-error mt-1">
                    {errors.ship_to_lat}
                  </p>
                )}
              </div>
              <div>
                <label
                  htmlFor="co-lon"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Longitude *
                </label>
                <input
                  id="co-lon"
                  type="number"
                  step="any"
                  value={form.ship_to_lon}
                  onChange={(e) => handleChange("ship_to_lon", e.target.value)}
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                />
                {errors.ship_to_lon && (
                  <p className="text-xs text-error mt-1">
                    {errors.ship_to_lon}
                  </p>
                )}
              </div>
              <div>
                <label
                  htmlFor="co-tank-id"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Tank ID
                </label>
                <input
                  id="co-tank-id"
                  type="text"
                  value={form.customer_tank_id}
                  onChange={(e) =>
                    handleChange("customer_tank_id", e.target.value)
                  }
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                />
              </div>
            </div>
          </fieldset>

          {/* Product & Volume */}
          <fieldset className="space-y-3">
            <legend className="text-sm font-medium text-gray-700">
              Product & Volume
            </legend>
            <div className="grid grid-cols-3 gap-3">
              <div>
                <label
                  htmlFor="co-product"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Product Code *
                </label>
                <ProductPicker
                  id="co-product"
                  aria-label="Product Code"
                  value={form.product_code || null}
                  onChange={(value) => handleChange("product_code", value)}
                  allowClear
                />
                {errors.product_code && (
                  <p className="text-xs text-error mt-1">
                    {errors.product_code}
                  </p>
                )}
              </div>
              <div>
                <label
                  htmlFor="co-gallons"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Gallons Requested
                </label>
                <input
                  id="co-gallons"
                  type="number"
                  step="any"
                  value={form.gallons_requested}
                  onChange={(e) =>
                    handleChange("gallons_requested", e.target.value)
                  }
                  disabled={form.fill_to_full}
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none disabled:bg-gray-100"
                />
                {errors.gallons_requested && (
                  <p className="text-xs text-error mt-1">
                    {errors.gallons_requested}
                  </p>
                )}
              </div>
              <div className="flex items-end pb-2">
                <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.fill_to_full}
                    onChange={(e) =>
                      handleChange("fill_to_full", e.target.checked)
                    }
                    className="rounded border-gray-300"
                  />
                  Fill to Full
                </label>
              </div>
            </div>
          </fieldset>

          {/* Scheduling */}
          <fieldset className="space-y-3">
            <legend className="text-sm font-medium text-gray-700">
              Scheduling
            </legend>
            <div className="grid grid-cols-3 gap-3">
              <div>
                <label
                  htmlFor="co-call-type"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Call Type *
                </label>
                <select
                  id="co-call-type"
                  value={form.call_type}
                  onChange={(e) => handleChange("call_type", e.target.value)}
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                >
                  <option value="one_off">One Off</option>
                  <option value="will_call">Will Call</option>
                  <option value="auto_fill">Auto Fill</option>
                  <option value="keep_full">Keep Full</option>
                </select>
              </div>
              <div>
                <label
                  htmlFor="co-window-start"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Window Start {form.call_type === "one_off" ? "*" : ""}
                </label>
                <input
                  id="co-window-start"
                  type="datetime-local"
                  value={form.delivery_window_start}
                  onChange={(e) =>
                    handleChange("delivery_window_start", e.target.value)
                  }
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                />
                {errors.delivery_window_start && (
                  <p className="text-xs text-error mt-1">
                    {errors.delivery_window_start}
                  </p>
                )}
              </div>
              <div>
                <label
                  htmlFor="co-window-end"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Window End {form.call_type === "one_off" ? "*" : ""}
                </label>
                <input
                  id="co-window-end"
                  type="datetime-local"
                  value={form.delivery_window_end}
                  onChange={(e) =>
                    handleChange("delivery_window_end", e.target.value)
                  }
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                />
                {errors.delivery_window_end && (
                  <p className="text-xs text-error mt-1">
                    {errors.delivery_window_end}
                  </p>
                )}
              </div>
            </div>
          </fieldset>

          {/* Optional fields */}
          <fieldset className="space-y-3">
            <legend className="text-sm font-medium text-gray-700">
              Additional
            </legend>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label
                  htmlFor="co-po"
                  className="block text-xs text-gray-600 mb-1"
                >
                  PO Number
                </label>
                <input
                  id="co-po"
                  type="text"
                  value={form.po_number}
                  onChange={(e) => handleChange("po_number", e.target.value)}
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                />
              </div>
              <div>
                <label
                  htmlFor="co-instructions"
                  className="block text-xs text-gray-600 mb-1"
                >
                  Special Instructions
                </label>
                <input
                  id="co-instructions"
                  type="text"
                  value={form.special_instructions}
                  onChange={(e) =>
                    handleChange("special_instructions", e.target.value)
                  }
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
                />
              </div>
            </div>
          </fieldset>

          {/* Actions */}
          <div className="flex items-center justify-end gap-3 pt-4 border-t border-gray-100">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-700 border border-gray-200 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white rounded-lg disabled:opacity-50 bg-primary hover:bg-primary-hover"
              aria-label="Submit order"
            >
              {submitting && <Loader2 className="w-4 h-4 animate-spin" />}
              Create Order
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
