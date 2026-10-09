"use client";

/**
 * CargoManifestEditor: the read-only CargoManifestView plus "Edit", which
 * opens an lg FormDialog with one row per cargo item (D9: every create/edit
 * flow on FormDialog).
 *
 * - Save calls updateCargo and the parent shows the updated manifest.
 * - Item status changes stay direct actions on the view (no input needed).
 * - On an API error the dialog stays open with the message and the user's
 *   edits (the FormDialog pattern), so nothing typed is lost (Req 7.6's
 *   "previous state" is the saved manifest, which is untouched until a save
 *   succeeds).
 * - Weights use a NumberField (kg, up to 2 decimals); a weight the user
 *   doesn't touch is sent back exactly as stored.
 *
 * Validates Requirements 7.1, 7.2, 7.3, 7.6.
 */

import { Pencil } from "lucide-react";
import { useCallback, useState } from "react";
import {
  Field,
  type FieldErrors,
  FormDialog,
  INPUT_CLASS,
  NumberField,
} from "@/components/ui";
import {
  updateCargo,
  updateCargoItemStatus,
} from "../../services/schedulingApi";
import type { CargoItemStatus, SchedulingCargoItem } from "../../types/api";
import CargoManifestView from "./CargoManifestView";

// ─── Props ───────────────────────────────────────────────────────────────────

interface CargoManifestEditorProps {
  /** The job ID this cargo manifest belongs to */
  jobId: string;
  /** Current cargo items */
  items: SchedulingCargoItem[];
  /** Callback when items are updated (so parent can sync state) */
  onItemsChange?: (items: SchedulingCargoItem[]) => void;
}

type ManifestValues = { items: SchedulingCargoItem[] };

/** Weight must be a number ≥ 0; description can't be blank. */
export function validateManifest(values: ManifestValues): FieldErrors {
  const errors: FieldErrors = {};
  for (const item of values.items) {
    const w = item.weight_kg as number | null;
    if (w == null || Number.isNaN(w) || w < 0)
      errors[`weight_${item.item_id}`] = "Enter a weight of 0 kg or more.";
    if (!item.description?.trim())
      errors[`description_${item.item_id}`] = "Enter a description.";
  }
  return errors;
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function CargoManifestEditor({
  jobId,
  items,
  onItemsChange,
}: CargoManifestEditorProps) {
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");

  const handleUpdateItemStatus = useCallback(
    async (itemId: string, newStatus: CargoItemStatus) => {
      setError("");
      try {
        const res = await updateCargoItemStatus(jobId, itemId, newStatus);
        const updatedItem = res.data;
        onItemsChange?.(
          items.map((item) => (item.item_id === itemId ? updatedItem : item)),
        );
      } catch (err) {
        setError(
          err instanceof Error
            ? err.message
            : "Failed to update cargo item status",
        );
      }
    },
    [jobId, items, onItemsChange],
  );

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center justify-between px-6 py-3 border-b border-gray-100">
        <h3 className="text-sm font-medium text-gray-600 uppercase tracking-wider">
          Cargo Manifest
        </h3>
        <button
          type="button"
          onClick={() => {
            setError("");
            setEditing(true);
          }}
          disabled={items.length === 0}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-700 hover:text-gray-900 rounded-lg hover:bg-gray-50 disabled:opacity-50"
          aria-label="Edit cargo manifest"
        >
          <Pencil className="w-4 h-4" aria-hidden="true" />
          Edit
        </button>
      </div>

      {error && (
        <div className="mx-6 mt-3">
          <p
            role="alert"
            className="text-sm text-error bg-error-light px-3 py-2 rounded-lg"
          >
            {error}
          </p>
        </div>
      )}

      <CargoManifestView
        items={items}
        onUpdateItemStatus={handleUpdateItemStatus}
      />

      <FormDialog<ManifestValues, SchedulingCargoItem[]>
        open={editing}
        size="lg"
        title="Edit cargo manifest"
        help="Change descriptions, weights, containers and seals. Item status changes stay on the manifest."
        submitLabel="Save manifest"
        successMessage="Cargo manifest saved"
        initialValues={{ items }}
        validate={validateManifest}
        onSubmit={async (v) => (await updateCargo(jobId, v.items)).data}
        onSaved={(updated) => onItemsChange?.(updated)}
        onClose={() => setEditing(false)}
      >
        {({ values, setValues, errors }) => {
          const patch = (
            itemId: string,
            change: Partial<SchedulingCargoItem>,
          ) =>
            setValues((prev) => ({
              items: prev.items.map((it) =>
                it.item_id === itemId ? { ...it, ...change } : it,
              ),
            }));
          return (
            <>
              {values.items.map((item) => (
                <fieldset
                  key={item.item_id}
                  className="col-span-2 grid grid-cols-2 gap-3 rounded-lg border border-slate-200 p-3"
                >
                  <legend className="px-1 text-xs font-semibold text-text-muted">
                    Item {item.item_id}
                  </legend>
                  <Field
                    label="Description"
                    required
                    error={errors[`description_${item.item_id}`]}
                  >
                    <input
                      type="text"
                      className={INPUT_CLASS}
                      value={item.description}
                      onChange={(e) =>
                        patch(item.item_id, { description: e.target.value })
                      }
                    />
                  </Field>
                  <Field
                    label="Weight"
                    required
                    span={1}
                    error={errors[`weight_${item.item_id}`]}
                    id={`cargo-weight-${item.item_id}`}
                  >
                    <NumberField
                      id={`cargo-weight-${item.item_id}`}
                      unit="kg"
                      min={0}
                      decimals={2}
                      value={item.weight_kg}
                      onChange={(n) =>
                        patch(item.item_id, { weight_kg: n as number })
                      }
                    />
                  </Field>
                  <Field label="Container" span={1}>
                    <input
                      type="text"
                      className={INPUT_CLASS}
                      value={item.container_number ?? ""}
                      onChange={(e) =>
                        patch(item.item_id, {
                          container_number: e.target.value,
                        })
                      }
                    />
                  </Field>
                  <Field label="Seal number" span={1}>
                    <input
                      type="text"
                      className={INPUT_CLASS}
                      value={item.seal_number ?? ""}
                      onChange={(e) =>
                        patch(item.item_id, { seal_number: e.target.value })
                      }
                    />
                  </Field>
                </fieldset>
              ))}
            </>
          );
        }}
      </FormDialog>
    </div>
  );
}
