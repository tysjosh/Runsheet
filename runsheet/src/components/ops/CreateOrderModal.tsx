"use client";

/**
 * Create order: the dispatcher's intake form, as a `FormDialog` (UI revamp
 * R6, design.md §5 "Order create": md, ProductSelect, NumberField).
 *
 * Posts to `POST /api/orders`. Validates client-side with the backend's rules
 * (`missing_volume`, `invalid_delivery_window`, `will_call` allows no window).
 * Envelope errors show inline (`details.fields` / FastAPI `details.errors`)
 * or as the form-level banner from `message`. `initialValues` prefills the
 * form (e.g. the Dashboard's low-tank "Create order").
 *
 * Validates: Requirements 2.4, 8.1.4
 */
import { useEffect, useMemo, useState } from "react";
import { listFuelProducts } from "../../services/fuelApi";
import {
  type CallType,
  type CreateOrderPayload,
  createOrder,
} from "../../services/ordersApi";
import { PRODUCT_CODES } from "../../styles/tokens";
import {
  Field,
  FormDialog,
  FormSection,
  INPUT_CLASS,
  NumberField,
  ProductSelect,
} from "../ui";
import CustomerPicker from "./CustomerPicker";

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
  /** Prefill (e.g. product and suggested gallons from a low tank). */
  initialValues?: Partial<CreateOrderFormValues>;
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
    errors.product_code = "Product is required";
  }

  if (!values.fill_to_full) {
    const gallons = parseFloat(values.gallons_requested);
    if (
      !values.gallons_requested.trim() ||
      Number.isNaN(gallons) ||
      gallons <= 0
    ) {
      errors.gallons_requested =
        "Gallons must be greater than 0 (or select Fill to full)";
    }
  }

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

export const INITIAL_FORM: CreateOrderFormValues = {
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

export function toPayload(form: CreateOrderFormValues): CreateOrderPayload {
  return {
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
}

/** The tenant's product catalog codes, falling back to the full token set. */
function useProductCodes(): string[] {
  const [codes, setCodes] = useState<string[]>([...PRODUCT_CODES]);
  useEffect(() => {
    let cancelled = false;
    listFuelProducts()
      .then((res) => {
        const list = (res?.items ?? [])
          .map((p: { product_code?: string }) => p.product_code)
          .filter(
            (c: unknown): c is string => typeof c === "string" && c.length > 0,
          );
        if (!cancelled && list.length > 0) setCodes(list);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  return codes;
}

const SECTIONS = [
  { id: "customer", title: "Customer" },
  { id: "location", title: "Delivery location" },
  { id: "product", title: "Product and volume" },
  { id: "schedule", title: "Scheduling" },
  { id: "more", title: "Additional" },
];

// ─── Component ───────────────────────────────────────────────────────────────

export default function CreateOrderModal({
  isOpen,
  onClose,
  onSuccess,
  initialValues,
}: CreateOrderModalProps) {
  const products = useProductCodes();
  const initial = useMemo(
    () => ({ ...INITIAL_FORM, ...initialValues }),
    [initialValues],
  );
  if (!isOpen) return null;
  return (
    <FormDialog<
      CreateOrderFormValues & Record<string, unknown>,
      { order_id?: string | null }
    >
      open
      size="md"
      title="Create order"
      submitLabel="Create order"
      initialValues={initial}
      sections={SECTIONS}
      validate={(v) => {
        const { general: _g, ...fields } = validateCreateOrderForm(v);
        return fields;
      }}
      onSubmit={(v) => createOrder(toPayload(v))}
      successMessage="Order created"
      onSaved={(res) => {
        if (res?.order_id) onSuccess?.(res.order_id);
      }}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <FormSection id="customer" title="Customer">
            <Field
              label="Customer ID"
              required
              error={errors.customer_id}
              span={1}
              id="co-customer-id"
            >
              <CustomerPicker
                id="co-customer-id"
                aria-label="Customer ID"
                value={values.customer_id || null}
                onChange={(value) => set("customer_id", value)}
                allowClear
              />
            </Field>
            <Field
              label="Customer name"
              required
              error={errors.customer_name}
              span={1}
            >
              <input
                id="co-customer-name"
                type="text"
                value={values.customer_name}
                onChange={(e) => set("customer_name", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="Phone" span={1}>
              <input
                id="co-phone"
                type="tel"
                value={values.customer_phone}
                onChange={(e) => set("customer_phone", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="Email" span={1}>
              <input
                id="co-email"
                type="email"
                value={values.customer_email}
                onChange={(e) => set("customer_email", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
          </FormSection>
          <FormSection id="location" title="Delivery location">
            <Field
              label="Ship-to address"
              required
              error={errors.ship_to_address}
            >
              <input
                id="co-address"
                type="text"
                value={values.ship_to_address}
                onChange={(e) => set("ship_to_address", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
            <Field
              label="Latitude"
              required
              error={errors.ship_to_lat}
              span={1}
            >
              <input
                id="co-lat"
                type="text"
                inputMode="decimal"
                value={values.ship_to_lat}
                onChange={(e) => set("ship_to_lat", e.target.value)}
                className={`${INPUT_CLASS} tabular-nums`}
              />
            </Field>
            <Field
              label="Longitude"
              required
              error={errors.ship_to_lon}
              span={1}
            >
              <input
                id="co-lon"
                type="text"
                inputMode="decimal"
                value={values.ship_to_lon}
                onChange={(e) => set("ship_to_lon", e.target.value)}
                className={`${INPUT_CLASS} tabular-nums`}
              />
            </Field>
            <Field label="Tank ID" span={1}>
              <input
                id="co-tank-id"
                type="text"
                value={values.customer_tank_id}
                onChange={(e) => set("customer_tank_id", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
          </FormSection>
          <FormSection id="product" title="Product and volume">
            <Field
              label="Product"
              required
              error={errors.product_code}
              id="co-product"
            >
              <ProductSelect
                id="co-product"
                aria-label="Product"
                value={values.product_code || null}
                options={products}
                onChange={(code) => set("product_code", code)}
              />
            </Field>
            <Field
              label="Gallons requested"
              required={!values.fill_to_full}
              error={errors.gallons_requested}
              span={1}
              id="co-gallons"
            >
              <NumberField
                id="co-gallons"
                unit="gal"
                min={0}
                value={
                  values.gallons_requested.trim() === ""
                    ? null
                    : Number(values.gallons_requested)
                }
                onChange={(n) =>
                  set(
                    "gallons_requested",
                    n === null || Number.isNaN(n) ? "" : String(n),
                  )
                }
                disabled={values.fill_to_full}
              />
            </Field>
            <div className="col-span-2 flex items-end pb-1.5 sm:col-span-1">
              <label className="flex cursor-pointer items-center gap-2 text-sm text-text">
                <input
                  type="checkbox"
                  checked={values.fill_to_full}
                  onChange={(e) => set("fill_to_full", e.target.checked)}
                  className="h-4 w-4 rounded border-slate-400 accent-[var(--rs-primary)]"
                />
                Fill to full
              </label>
            </div>
          </FormSection>
          <FormSection id="schedule" title="Scheduling">
            <Field label="Call type" required>
              <select
                id="co-call-type"
                value={values.call_type}
                onChange={(e) => set("call_type", e.target.value as CallType)}
                className={INPUT_CLASS}
              >
                <option value="one_off">One off</option>
                <option value="will_call">Will call</option>
                <option value="auto_fill">Auto fill</option>
                <option value="keep_full">Keep full</option>
              </select>
            </Field>
            <Field
              label="Window start"
              required={values.call_type === "one_off"}
              error={errors.delivery_window_start}
              span={1}
            >
              <input
                id="co-window-start"
                type="datetime-local"
                value={values.delivery_window_start}
                onChange={(e) => set("delivery_window_start", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
            <Field
              label="Window end"
              required={values.call_type === "one_off"}
              error={errors.delivery_window_end}
              span={1}
            >
              <input
                id="co-window-end"
                type="datetime-local"
                value={values.delivery_window_end}
                onChange={(e) => set("delivery_window_end", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
          </FormSection>
          <FormSection id="more" title="Additional">
            <Field label="PO number" span={1}>
              <input
                id="co-po"
                type="text"
                value={values.po_number}
                onChange={(e) => set("po_number", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="Special instructions" span={1}>
              <input
                id="co-instructions"
                type="text"
                value={values.special_instructions}
                onChange={(e) => set("special_instructions", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
          </FormSection>
        </>
      )}
    </FormDialog>
  );
}
