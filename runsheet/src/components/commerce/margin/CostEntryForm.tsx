"use client";

/**
 * Add a cost entry, or supersede one (the same form prefilled, plus a
 * required reason), as an md FormDialog (UI revamp task 3.4, design.md §5).
 * Money is sent as the typed decimal string so nothing is rounded in the
 * browser (the API validates 6 dp, at most $100.000000), so the unit cost
 * stays a decimal text input rather than a rounding NumberField.
 */
import { ApiError } from "../../../services/api";
import {
  type CostEntry,
  type CostEntryKind,
  type CostEntryPayload,
  createCostEntry,
  supersedeCostEntry,
} from "../../../services/marginApi";
import { PRODUCT_CODES } from "../../../styles/tokens";
import {
  Field,
  FormDialog,
  INPUT_CLASS,
  ProductSelect,
  Select,
} from "../../ui";
import { microsToDecimalString, milliToDecimalString } from "./marginFormat";

/** ISO instant -> `YYYY-MM-DDTHH:mm` in local time for `datetime-local`. */
function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

type Draft = {
  reason: string;
  kind: CostEntryKind;
  product_code: string;
  terminal_id: string;
  supplier_name: string;
  effective_at: string;
  effective_to: string;
  unit_cost_usd: string;
  gallons: string;
  adder_type: "" | "freight" | "fee" | "other";
  bol_id: string;
  reference: string;
  notes: string;
};

function draftFrom(entry?: CostEntry): Draft {
  return {
    reason: "",
    kind: entry?.kind ?? "purchase",
    product_code: entry?.product_code ?? "",
    terminal_id: entry?.terminal_id ?? "",
    supplier_name: entry?.supplier_name ?? "",
    effective_at: toLocalInput(entry?.effective_at ?? null),
    effective_to: toLocalInput(entry?.effective_to ?? null),
    unit_cost_usd: entry ? microsToDecimalString(entry.unit_cost_micros) : "",
    gallons:
      entry?.gallons_milli != null
        ? milliToDecimalString(entry.gallons_milli)
        : "",
    adder_type: entry?.adder_type ?? "",
    bol_id: entry?.bol_id ?? "",
    reference: entry?.reference ?? "",
    notes: entry?.notes ?? "",
  };
}

function toPayload(d: Draft): CostEntryPayload {
  const optional = (v: string) => (v.trim() ? v.trim() : undefined);
  return {
    kind: d.kind,
    product_code: d.product_code.trim(),
    terminal_id: optional(d.terminal_id),
    supplier_name: optional(d.supplier_name),
    effective_at: new Date(d.effective_at).toISOString(),
    effective_to: d.effective_to
      ? new Date(d.effective_to).toISOString()
      : undefined,
    unit_cost_usd: d.unit_cost_usd.trim(),
    gallons: optional(d.gallons),
    adder_type: d.kind === "adder" && d.adder_type ? d.adder_type : undefined,
    bol_id: optional(d.bol_id),
    reference: optional(d.reference),
    notes: optional(d.notes),
  };
}

export function apiFieldErrors(error: unknown): string[] {
  if (error instanceof ApiError) {
    const errors =
      (error.details?.errors as
        | { loc?: unknown[]; msg?: string; row?: number }[]
        | undefined) ?? [];
    if (errors.length > 0) {
      return errors.map((e) => {
        const where = (e.loc ?? []).filter((p) => p !== "body").join(".");
        return `${e.row ? `Row ${e.row}: ` : ""}${where ? `${where}: ` : ""}${e.msg ?? "invalid"}`;
      });
    }
    if (error.status === 409)
      return ["An active entry like this already exists."];
    return [error.message || "The entry could not be saved."];
  }
  return ["The entry could not be saved. Try again."];
}

export interface CostEntryFormProps {
  /** When set, the form supersedes this entry and asks for a reason. */
  entry?: CostEntry;
  onSaved: (entry: CostEntry, warnings: string[]) => void;
  onClose: () => void;
}

const DECIMAL = /^\d+(\.\d+)?$/;

export function validateCostEntry(d: Draft, superseding: boolean) {
  const errors: Record<string, string | undefined> = {};
  if (!d.product_code.trim()) errors.product_code = "Pick a product.";
  if (!d.effective_at || Number.isNaN(new Date(d.effective_at).getTime()))
    errors.effective_at = "Enter the date the cost takes effect.";
  if (
    d.effective_to &&
    d.effective_at &&
    new Date(d.effective_to) <= new Date(d.effective_at)
  )
    errors.effective_to = "End must be after the start.";
  if (!d.unit_cost_usd.trim()) errors.unit_cost_usd = "Enter the unit cost.";
  else if (!DECIMAL.test(d.unit_cost_usd.trim()))
    errors.unit_cost_usd = "Use digits and a decimal point, e.g. 2.4500.";
  if (d.gallons.trim() && !DECIMAL.test(d.gallons.trim()))
    errors.gallons = "Use digits and a decimal point.";
  if (superseding && !d.reason.trim())
    errors.reason = "Enter a reason for the change.";
  return errors;
}

export default function CostEntryForm({
  entry,
  onSaved,
  onClose,
}: CostEntryFormProps) {
  const superseding = entry !== undefined;

  const submit = async (draft: Draft) => {
    try {
      const payload = toPayload(draft);
      return entry
        ? await supersedeCostEntry(entry.entry_id, {
            ...payload,
            reason: draft.reason.trim(),
          })
        : await createCostEntry(payload);
    } catch (e) {
      // FastAPI 422s map onto fields inside the dialog; a 409 or a row
      // error without a field becomes the dialog's banner.
      if (e instanceof ApiError && e.status !== 409) throw e;
      throw new Error(apiFieldErrors(e).join(" "));
    }
  };

  const products = (code: string) =>
    code && !(PRODUCT_CODES as readonly string[]).includes(code)
      ? [...PRODUCT_CODES, code]
      : [...PRODUCT_CODES];

  return (
    <FormDialog<Draft, Awaited<ReturnType<typeof submit>>>
      open
      size="md"
      title={superseding ? "Supersede cost entry" : "Add cost entry"}
      help={
        superseding
          ? "Saves a new version; the current entry is kept as superseded."
          : undefined
      }
      submitLabel={superseding ? "Save new version" : "Add entry"}
      successMessage={
        superseding ? "Cost entry superseded" : "Cost entry added"
      }
      initialValues={draftFrom(entry)}
      validate={(d) => validateCostEntry(d, superseding)}
      onSubmit={submit}
      onSaved={(result) => onSaved(result.entry, result.warnings)}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field label="Kind" span={1}>
            <Select
              id="cost-kind"
              value={values.kind}
              disabled={superseding}
              onChange={(v) => set("kind", v as CostEntryKind)}
              options={[
                { value: "purchase", label: "Purchase" },
                { value: "override", label: "Override" },
                { value: "adder", label: "Adder (freight, fees)" },
              ]}
            />
          </Field>
          <Field label="Product" required span={1} error={errors.product_code}>
            <ProductSelect
              id="cost-product"
              value={values.product_code || null}
              options={products(values.product_code)}
              onChange={(code) => set("product_code", code)}
            />
          </Field>
          <Field label="Terminal" span={1} help="Blank for tenant-wide">
            <input
              id="cost-terminal"
              className={INPUT_CLASS}
              value={values.terminal_id}
              onChange={(e) => set("terminal_id", e.target.value)}
            />
          </Field>
          <Field label="Supplier" span={1}>
            <input
              id="cost-supplier"
              className={INPUT_CLASS}
              value={values.supplier_name}
              onChange={(e) => set("supplier_name", e.target.value)}
            />
          </Field>
          <Field
            label="Effective from"
            required
            span={1}
            error={errors.effective_at}
          >
            <input
              id="cost-effective-at"
              type="datetime-local"
              className={INPUT_CLASS}
              value={values.effective_at}
              onChange={(e) => set("effective_at", e.target.value)}
            />
          </Field>
          <Field label="Effective to" span={1} error={errors.effective_to}>
            <input
              id="cost-effective-to"
              type="datetime-local"
              className={INPUT_CLASS}
              value={values.effective_to}
              onChange={(e) => set("effective_to", e.target.value)}
            />
          </Field>
          <Field
            label="Unit cost (USD per gallon)"
            required
            span={1}
            error={errors.unit_cost_usd}
            help="Up to 6 decimals"
          >
            <input
              id="cost-unit"
              inputMode="decimal"
              className={INPUT_CLASS}
              value={values.unit_cost_usd}
              onChange={(e) => set("unit_cost_usd", e.target.value)}
              placeholder="2.450000"
            />
          </Field>
          <Field
            label="Gallons"
            span={1}
            error={errors.gallons}
            help="Purchases only"
          >
            <input
              id="cost-gallons"
              inputMode="decimal"
              className={INPUT_CLASS}
              value={values.gallons}
              onChange={(e) => set("gallons", e.target.value)}
            />
          </Field>
          {values.kind === "adder" && (
            <Field label="Adder type" span={1}>
              <Select
                id="cost-adder"
                value={values.adder_type}
                onChange={(v) => set("adder_type", v as Draft["adder_type"])}
                placeholder="Choose…"
                options={[
                  { value: "freight", label: "Freight" },
                  { value: "fee", label: "Fee" },
                  { value: "other", label: "Other" },
                ]}
              />
            </Field>
          )}
          <Field label="BOL ID" span={1}>
            <input
              id="cost-bol"
              className={INPUT_CLASS}
              value={values.bol_id}
              onChange={(e) => set("bol_id", e.target.value)}
            />
          </Field>
          <Field label="Reference" span={1}>
            <input
              id="cost-reference"
              className={INPUT_CLASS}
              value={values.reference}
              onChange={(e) => set("reference", e.target.value)}
            />
          </Field>
          <Field label="Notes">
            <textarea
              id="cost-notes"
              rows={2}
              maxLength={500}
              className={`${INPUT_CLASS} h-auto py-1.5`}
              value={values.notes}
              onChange={(e) => set("notes", e.target.value)}
            />
          </Field>
          {superseding && (
            <Field label="Reason for the change" required error={errors.reason}>
              <textarea
                id="cost-reason"
                rows={2}
                maxLength={500}
                className={`${INPUT_CLASS} h-auto py-1.5`}
                value={values.reason}
                onChange={(e) => set("reason", e.target.value)}
              />
            </Field>
          )}
        </>
      )}
    </FormDialog>
  );
}
