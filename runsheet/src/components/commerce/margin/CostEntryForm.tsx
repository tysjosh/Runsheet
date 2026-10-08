"use client";

/**
 * Add a cost entry, or supersede one (the same form prefilled, plus a
 * required reason). Money is sent as the typed decimal string so nothing is
 * rounded in the browser; the API validates it (6 dp, at most $100.000000).
 */
import { type FormEvent, useState } from "react";
import { ApiError } from "../../../services/api";
import {
  type CostEntry,
  type CostEntryKind,
  type CostEntryPayload,
  createCostEntry,
  supersedeCostEntry,
} from "../../../services/marginApi";
import { Button } from "../../ui";
import { microsToDecimalString, milliToDecimalString } from "./marginFormat";

const inputClass =
  "mt-1 block w-full rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40";

/** ISO instant -> `YYYY-MM-DDTHH:mm` in local time for `datetime-local`. */
function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

interface Draft {
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
}

function draftFrom(entry?: CostEntry): Draft {
  return {
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
  onCancel: () => void;
}

export default function CostEntryForm({
  entry,
  onSaved,
  onCancel,
}: CostEntryFormProps) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(entry));
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const superseding = entry !== undefined;

  const set = (name: keyof Draft) => (e: { target: { value: string } }) =>
    setDraft((d) => ({ ...d, [name]: e.target.value }));

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (
      !draft.effective_at ||
      Number.isNaN(new Date(draft.effective_at).getTime())
    ) {
      setErrors(["Enter the date the cost takes effect."]);
      return;
    }
    if (superseding && !reason.trim()) {
      setErrors(["Enter a reason for the change."]);
      return;
    }
    setSaving(true);
    setErrors([]);
    try {
      const payload = toPayload(draft);
      const result = entry
        ? await supersedeCostEntry(entry.entry_id, {
            ...payload,
            reason: reason.trim(),
          })
        : await createCostEntry(payload);
      onSaved(result.entry, result.warnings);
    } catch (e) {
      setErrors(apiFieldErrors(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      onSubmit={onSubmit}
      className="grid grid-cols-2 gap-3"
      aria-label={superseding ? "Supersede cost entry" : "Add cost entry"}
    >
      <label className="text-sm">
        Kind
        <select
          className={inputClass}
          value={draft.kind}
          onChange={set("kind")}
          disabled={superseding}
        >
          <option value="purchase">Purchase</option>
          <option value="override">Override</option>
          <option value="adder">Adder (freight, fees)</option>
        </select>
      </label>
      <label className="text-sm">
        Product
        <input
          required
          className={inputClass}
          value={draft.product_code}
          onChange={set("product_code")}
        />
      </label>
      <label className="text-sm">
        Terminal (blank for tenant-wide)
        <input
          className={inputClass}
          value={draft.terminal_id}
          onChange={set("terminal_id")}
        />
      </label>
      <label className="text-sm">
        Supplier
        <input
          className={inputClass}
          value={draft.supplier_name}
          onChange={set("supplier_name")}
        />
      </label>
      <label className="text-sm">
        Effective from
        <input
          required
          type="datetime-local"
          className={inputClass}
          value={draft.effective_at}
          onChange={set("effective_at")}
        />
      </label>
      <label className="text-sm">
        Effective to (optional)
        <input
          type="datetime-local"
          className={inputClass}
          value={draft.effective_to}
          onChange={set("effective_to")}
        />
      </label>
      <label className="text-sm">
        Unit cost (USD per gallon)
        <input
          required
          inputMode="decimal"
          className={inputClass}
          value={draft.unit_cost_usd}
          onChange={set("unit_cost_usd")}
          placeholder="2.450000"
        />
      </label>
      <label className="text-sm">
        Gallons (purchases)
        <input
          inputMode="decimal"
          className={inputClass}
          value={draft.gallons}
          onChange={set("gallons")}
        />
      </label>
      {draft.kind === "adder" && (
        <label className="text-sm">
          Adder type
          <select
            className={inputClass}
            value={draft.adder_type}
            onChange={set("adder_type")}
          >
            <option value="">Choose…</option>
            <option value="freight">Freight</option>
            <option value="fee">Fee</option>
            <option value="other">Other</option>
          </select>
        </label>
      )}
      <label className="text-sm">
        BOL id (optional)
        <input
          className={inputClass}
          value={draft.bol_id}
          onChange={set("bol_id")}
        />
      </label>
      <label className="text-sm">
        Reference
        <input
          className={inputClass}
          value={draft.reference}
          onChange={set("reference")}
        />
      </label>
      <label className="text-sm col-span-2">
        Notes
        <textarea
          className={inputClass}
          value={draft.notes}
          onChange={set("notes")}
          maxLength={500}
        />
      </label>
      {superseding && (
        <label className="text-sm col-span-2">
          Reason for the change
          <textarea
            required
            className={inputClass}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={500}
          />
        </label>
      )}
      <div
        role="alert"
        className={errors.length ? "col-span-2 text-sm text-error" : "sr-only"}
      >
        {errors.length > 0 && (
          <ul className="list-disc pl-5">
            {errors.map((msg) => (
              <li key={msg}>{msg}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="col-span-2 flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" loading={saving}>
          {superseding ? "Save new version" : "Add entry"}
        </Button>
      </div>
    </form>
  );
}
