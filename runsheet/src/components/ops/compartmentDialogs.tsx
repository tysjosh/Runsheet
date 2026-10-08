"use client";

/**
 * Truck compartment dialogs (UI revamp task 3.1, design.md §5 "Truck
 * compartments": lg with rows; NumberField). All three were bespoke
 * `fixed inset-0` overlays:
 *
 * - ConfigureCompartmentsDialog: lg FormDialog with one row per compartment.
 *   Capacity is a whole-gallon NumberField (the old `type=number` field could
 *   show a raw float such as 5283.441047162968); allowed grades are product
 *   chips (cap + readable name, code on hover) instead of a comma-separated
 *   code list. A capacity the user didn't touch is sent back at its exact
 *   stored value, so saving doesn't round a litre-based compartment.
 * - CleaningEventDialog: md FormDialog (method, actor, driver, notes,
 *   evidence uploads through the presigned contract).
 * - LoadEligibilityDialog: a check, not a create/edit, so it stays a plain
 *   `Modal`, now with a ProductSelect instead of a typed product code.
 */
import {
  AlertTriangle,
  Check,
  Image as ImageIcon,
  Loader2,
  Plus,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { productName } from "../../lib/format";
import { ApiError } from "../../services/api";
import {
  type PodUploadContentType,
  presignPodUpload,
  putPresignedFile,
} from "../../services/driverApi";
import type {
  CleaningEvent,
  CleaningMethod,
  CompartmentConfigInput,
  LoadEligibilityDecision,
  LoadEligibilityResponse,
  TruckCompartmentState,
} from "../../services/fuelApi";
import {
  checkCompartmentLoadEligibility,
  configureCompartments,
  getTruckCompartmentCapacityGallons,
  recordCleaningEvent,
} from "../../services/fuelApi";
import type { StatusKey } from "../../styles/tokens";
import { PRODUCT_CODES } from "../../styles/tokens";
import {
  Button,
  Field,
  FormDialog,
  INPUT_CLASS,
  Modal,
  NumberField,
  ProductCap,
  ProductChip,
  ProductSelect,
  Select,
  StatusBadge,
} from "../ui";

// ─── Cleaning event ──────────────────────────────────────────────────────────

export const CLEANING_METHODS: { value: CleaningMethod; label: string }[] = [
  { value: "flush", label: "Flush" },
  { value: "purge", label: "Purge" },
  { value: "sanitize", label: "Sanitize" },
];

/** Mirrors the backend's ``_POD_UPLOAD_ALLOWED_MIME_TYPES``. */
const EVIDENCE_UPLOAD_TYPES: Record<string, PodUploadContentType> = {
  "image/jpeg": "image/jpeg",
  "image/png": "image/png",
  "image/heic": "image/heic",
  "application/pdf": "application/pdf",
};
const EVIDENCE_ACCEPT = Object.keys(EVIDENCE_UPLOAD_TYPES).join(",");

/** Pure validator for the cleaning-event form; exported for unit tests. */
export interface CleaningFormValues {
  method: CleaningMethod;
  actor_id: string;
  driver_id?: string;
  notes: string;
  [key: string]: unknown;
}

export interface CleaningFormErrors {
  actor_id?: string;
  method?: string;
  [key: string]: string | undefined;
}

export function validateCleaningForm(
  values: CleaningFormValues,
): CleaningFormErrors {
  const errors: CleaningFormErrors = {};
  if (!values.actor_id || !values.actor_id.trim()) {
    errors.actor_id = "Actor ID is required.";
  }
  if (!CLEANING_METHODS.some((m) => m.value === values.method)) {
    errors.method = "Method must be flush, purge, or sanitize.";
  }
  return errors;
}

interface EvidenceItem {
  id: number;
  file: File;
  status: "queued" | "uploading" | "uploaded" | "error";
  file_ref?: string;
  error?: string;
}

let evidenceIdCounter = 0;

export function CleaningEventDialog({
  compartment,
  onClose,
  onSuccess,
}: {
  compartment: TruckCompartmentState;
  onClose: () => void;
  onSuccess: (event: CleaningEvent) => void;
}) {
  const [evidence, setEvidence] = useState<EvidenceItem[]>([]);
  const uploadedRefs = useMemo(
    () =>
      evidence
        .filter((e) => e.status === "uploaded" && e.file_ref)
        .map((e) => e.file_ref as string),
    [evidence],
  );
  const anyUploading = evidence.some((e) => e.status === "uploading");
  const anyPending = evidence.some(
    (e) => e.status === "queued" || e.status === "error",
  );

  const patch = (id: number, p: Partial<EvidenceItem>) =>
    setEvidence((prev) => prev.map((e) => (e.id === id ? { ...e, ...p } : e)));

  async function uploadSingle(item: EvidenceItem) {
    const contentType = EVIDENCE_UPLOAD_TYPES[item.file.type];
    if (!contentType) {
      patch(item.id, { status: "error", error: "Unsupported file type" });
      return;
    }
    patch(item.id, { status: "uploading", error: undefined });
    try {
      const presigned = await presignPodUpload("photo", contentType);
      if (item.file.size > presigned.max_file_bytes) {
        throw new Error(
          `File exceeds tenant limit of ${Math.round(presigned.max_file_bytes / 1_000_000)} MB.`,
        );
      }
      await putPresignedFile(presigned.upload_url, item.file, contentType);
      patch(item.id, { status: "uploaded", file_ref: presigned.file_ref });
    } catch (err) {
      patch(item.id, {
        status: "error",
        error: err instanceof Error ? err.message : "Upload failed",
      });
    }
  }

  function handleFileSelect(files: FileList | null) {
    if (!files) return;
    const next: EvidenceItem[] = Array.from(files).map((file) => ({
      id: ++evidenceIdCounter,
      file,
      status: "queued",
    }));
    setEvidence((prev) => [...prev, ...next]);
    // Sequential uploads so we don't saturate the network.
    void (async () => {
      for (const item of next) await uploadSingle(item);
    })();
  }

  const submit = async (v: CleaningFormValues): Promise<CleaningEvent> => {
    if (anyUploading)
      throw new Error("Wait for evidence uploads to finish before saving.");
    if (anyPending)
      throw new Error("Retry or remove queued evidence uploads before saving.");
    return recordCleaningEvent(compartment.compartment_id, {
      method: v.method,
      actor_id: v.actor_id.trim(),
      driver_id: v.driver_id?.trim() ? v.driver_id.trim() : undefined,
      notes: v.notes.trim() ? v.notes.trim() : undefined,
      evidence_refs: uploadedRefs,
    });
  };

  return (
    <FormDialog<CleaningFormValues, CleaningEvent>
      open
      size="md"
      title="Record cleaning event"
      help={`${compartment.truck_id} · Compartment ${compartment.compartment_id}`}
      submitLabel="Record cleaning"
      successMessage={null}
      initialValues={{
        method: "flush",
        actor_id: "",
        driver_id: "",
        notes: "",
      }}
      validate={validateCleaningForm}
      onSubmit={submit}
      onSaved={onSuccess}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field label="Method" error={errors.method} span={1} id="ce-method">
            <Select
              id="ce-method"
              value={values.method}
              onChange={(m) => set("method", m as CleaningMethod)}
              options={CLEANING_METHODS}
            />
          </Field>
          <Field label="Actor ID" required error={errors.actor_id} span={1}>
            <input
              id="ce-actor"
              type="text"
              className={INPUT_CLASS}
              value={values.actor_id}
              onChange={(e) => set("actor_id", e.target.value)}
              placeholder="e.g. driver-042"
            />
          </Field>
          <Field
            label="Driver"
            help="Driver reference from the Drivers module. Preferred over the actor ID, which is kept for older records."
          >
            <input
              id="ce-driver"
              type="text"
              className={INPUT_CLASS}
              value={values.driver_id ?? ""}
              onChange={(e) => set("driver_id", e.target.value)}
              placeholder="e.g. DRV-001"
            />
          </Field>
          <Field label="Notes">
            <textarea
              id="ce-notes"
              rows={3}
              className={INPUT_CLASS}
              value={values.notes}
              onChange={(e) => set("notes", e.target.value)}
              placeholder="Anything the next driver should know."
            />
          </Field>
          <div className="col-span-2">
            <p className="mb-1 text-xs font-medium text-slate-700">
              Evidence photos
            </p>
            <label
              htmlFor="ce-evidence"
              className="flex cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed border-slate-300 px-4 py-4 text-sm text-slate-700 hover:bg-slate-50"
            >
              <Upload className="h-5 w-5" aria-hidden="true" />
              <span>Select photos or PDFs</span>
              <span className="text-xs text-text-muted">
                JPEG · PNG · HEIC · PDF
              </span>
              <input
                id="ce-evidence"
                type="file"
                className="sr-only"
                accept={EVIDENCE_ACCEPT}
                multiple
                data-testid="cleaning-event-evidence-input"
                onChange={(e) => {
                  handleFileSelect(e.target.files);
                  e.target.value = "";
                }}
              />
            </label>
            {evidence.length > 0 && (
              <ul className="mt-2 space-y-1.5">
                {evidence.map((item) => (
                  <li
                    key={item.id}
                    className="flex items-center gap-2 rounded-md border border-slate-200 px-3 py-1.5 text-xs"
                  >
                    <ImageIcon
                      className="h-3.5 w-3.5 text-slate-500"
                      aria-hidden="true"
                    />
                    <span className="flex-1 truncate text-slate-700">
                      {item.file.name}
                    </span>
                    {item.status === "uploading" && (
                      <span className="inline-flex items-center gap-1 text-blue-800">
                        <Loader2
                          className="h-3 w-3 animate-spin"
                          aria-hidden="true"
                        />
                        Uploading
                      </span>
                    )}
                    {item.status === "uploaded" && (
                      <span className="inline-flex items-center gap-1 text-green-800">
                        <Check className="h-3 w-3" aria-hidden="true" />
                        Uploaded
                      </span>
                    )}
                    {item.status === "error" && (
                      <span className="inline-flex items-center gap-1 text-red-800">
                        <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                        {item.error || "Failed"}
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={() =>
                        setEvidence((prev) =>
                          prev.filter((e) => e.id !== item.id),
                        )
                      }
                      className="rounded p-0.5 text-slate-500 hover:text-slate-800"
                      aria-label={`Remove ${item.file.name}`}
                    >
                      <X className="h-3 w-3" aria-hidden="true" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </FormDialog>
  );
}

// ─── Load eligibility ────────────────────────────────────────────────────────

/** Badge style per eligibility decision (icon + label from StatusBadge). */
export const ELIGIBILITY_DECISION_CONFIG: Record<
  LoadEligibilityDecision,
  { label: string; status: StatusKey }
> = {
  allowed: { label: "Allowed", status: "ok" },
  blocked: { label: "Blocked", status: "critical" },
  requires_cleaning: { label: "Requires cleaning", status: "warning" },
};

export function LoadEligibilityDialog({
  compartment,
  onClose,
}: {
  compartment: TruckCompartmentState;
  onClose: () => void;
}) {
  const [productCode, setProductCode] = useState<string | null>(null);
  const [result, setResult] = useState<LoadEligibilityResponse | null>(null);
  const [apiError, setApiError] = useState("");
  const [checking, setChecking] = useState(false);

  async function handleCheck(e: React.FormEvent) {
    e.preventDefault();
    if (!productCode) {
      setApiError("Choose a product to check.");
      return;
    }
    setApiError("");
    setChecking(true);
    try {
      setResult(
        await checkCompartmentLoadEligibility(
          compartment.compartment_id,
          productCode,
        ),
      );
    } catch (err) {
      setResult(null);
      setApiError(
        err instanceof ApiError
          ? err.message || `Request failed (HTTP ${err.status}).`
          : err instanceof Error
            ? err.message
            : "Failed to check eligibility.",
      );
    } finally {
      setChecking(false);
    }
  }

  const decision = result
    ? (ELIGIBILITY_DECISION_CONFIG[result.decision] ??
      ELIGIBILITY_DECISION_CONFIG.allowed)
    : null;
  const governing = result
    ? (ELIGIBILITY_DECISION_CONFIG[result.governing_rule] ??
      ELIGIBILITY_DECISION_CONFIG.allowed)
    : null;

  return (
    <Modal
      isOpen
      onClose={onClose}
      title="Check load eligibility"
      closeLabel="Close load eligibility form"
      size="md"
      subheader={
        <p className="text-xs text-text-muted">
          Compartment {compartment.compartment_id} · Last loaded{" "}
          <span className="font-medium text-text">
            {compartment.last_loaded_product
              ? productName(compartment.last_loaded_product)
              : "none"}
          </span>
        </p>
      }
    >
      <form
        onSubmit={handleCheck}
        className="space-y-4"
        data-testid="load-eligibility-form"
      >
        {apiError && (
          <p
            role="alert"
            className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800"
          >
            {apiError}
          </p>
        )}
        <Field label="Proposed product" id="le-product">
          <ProductSelect
            id="le-product"
            value={productCode}
            options={PRODUCT_CODES}
            placeholder="Choose a product"
            onChange={setProductCode}
          />
        </Field>
        <div className="flex justify-end">
          <Button
            type="submit"
            loading={checking}
            icon={<Search className="h-3.5 w-3.5" aria-hidden="true" />}
          >
            Check
          </Button>
        </div>
      </form>
      {result && decision && governing && (
        <div
          className="mt-4 space-y-3 rounded-lg border border-slate-200 p-4"
          data-testid="load-eligibility-result"
        >
          <div className="flex items-center gap-2">
            <span className="text-xs text-text-muted">Decision</span>
            <span data-testid={`load-eligibility-decision-${result.decision}`}>
              <StatusBadge status={decision.status} label={decision.label} />
            </span>
          </div>
          <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-xs text-text-muted">Proposed product</dt>
              <dd>
                <ProductChip code={result.proposed_product} variant="full" />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-text-muted">Previous product</dt>
              <dd>
                {result.previous_product ? (
                  <ProductChip code={result.previous_product} variant="full" />
                ) : (
                  "—"
                )}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-text-muted">Governing rule</dt>
              <dd
                data-testid={`load-eligibility-governing-${result.governing_rule}`}
              >
                <StatusBadge
                  status={governing.status}
                  label={governing.label}
                />
              </dd>
            </div>
            {result.reason && (
              <div className="sm:col-span-2">
                <dt className="text-xs text-text-muted">Reason</dt>
                <dd
                  className="text-slate-700"
                  data-testid="load-eligibility-reason"
                >
                  {result.reason}
                </dd>
              </div>
            )}
          </dl>
        </div>
      )}
    </Modal>
  );
}

// ─── Configure compartments ──────────────────────────────────────────────────

interface CompartmentRowValue {
  compartment_id: string;
  capacity_gallons: number | null;
  /** Stored capacity (exact) for an existing compartment. */
  stored_gallons?: number;
  allowed_grades: string[];
  position_index: number | null;
}

type ConfigureValues = {
  truck_id: string;
  rows: CompartmentRowValue[];
};

const emptyRow = (position: number): CompartmentRowValue => ({
  compartment_id: "",
  capacity_gallons: null,
  allowed_grades: [],
  position_index: position,
});

export function validateCompartments(v: ConfigureValues) {
  const errors: Record<string, string | undefined> = {};
  if (!v.truck_id.trim()) errors.truck_id = "Enter the truck ID.";
  v.rows.forEach((r, i) => {
    if (!r.compartment_id.trim())
      errors[`rows.${i}.compartment_id`] = "Enter an ID.";
    if (
      r.capacity_gallons == null ||
      Number.isNaN(r.capacity_gallons) ||
      r.capacity_gallons <= 0
    )
      errors[`rows.${i}.capacity_gallons`] = "Capacity must be above 0.";
    if (r.allowed_grades.length === 0)
      errors[`rows.${i}.allowed_grades`] = "Pick at least one product.";
  });
  if (v.rows.length === 0) errors.rows = "Add at least one compartment.";
  return errors;
}

/** The PUT body; untouched capacities go back at their exact stored value. */
export function toCompartmentConfig(
  rows: CompartmentRowValue[],
): CompartmentConfigInput[] {
  return rows.map((r, i) => ({
    compartment_id: r.compartment_id.trim(),
    capacity_gallons:
      r.stored_gallons !== undefined &&
      r.capacity_gallons === Math.round(r.stored_gallons)
        ? r.stored_gallons
        : (r.capacity_gallons as number),
    allowed_grades: r.allowed_grades,
    position_index: r.position_index ?? i,
  }));
}

export function ConfigureCompartmentsDialog({
  initialTruckId,
  lockTruckId,
  initialCompartments,
  onClose,
  onSuccess,
}: {
  initialTruckId?: string;
  lockTruckId?: boolean;
  initialCompartments?: TruckCompartmentState[];
  onClose: () => void;
  onSuccess: (truckId: string, count: number) => void;
}) {
  const initial = useMemo<ConfigureValues>(
    () => ({
      truck_id: initialTruckId ?? "",
      rows:
        initialCompartments && initialCompartments.length > 0
          ? initialCompartments.map((c, i) => {
              const g = getTruckCompartmentCapacityGallons(c);
              return {
                compartment_id: c.compartment_id,
                capacity_gallons: Number.isNaN(g) ? null : Math.round(g),
                stored_gallons: Number.isNaN(g) ? undefined : g,
                allowed_grades: [...c.allowed_grades],
                position_index: c.position_index ?? i,
              };
            })
          : [emptyRow(0)],
    }),
    [initialTruckId, initialCompartments],
  );

  const submit = async (v: ConfigureValues) => {
    try {
      return await configureCompartments(
        v.truck_id.trim(),
        toCompartmentConfig(v.rows),
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        // Unknown grades are rejected with a 400 naming the value.
        throw new ApiError(
          `${err.message}. Pick products from the list.`,
          err.status,
          err.code,
          err.details,
        );
      }
      throw err;
    }
  };

  return (
    <FormDialog<ConfigureValues, Awaited<ReturnType<typeof submit>>>
      open
      size="lg"
      title={lockTruckId ? "Edit compartments" : "Configure compartments"}
      submitLabel="Save compartments"
      successMessage={null}
      initialValues={initial}
      validate={validateCompartments}
      onSubmit={submit}
      onSaved={(r) => onSuccess(r.truck_id, r.compartments_configured)}
      onClose={onClose}
    >
      {({ values, set, errors }) => {
        const setRow = (idx: number, p: Partial<CompartmentRowValue>) =>
          set(
            "rows",
            values.rows.map((r, i) => (i === idx ? { ...r, ...p } : r)),
          );
        return (
          <>
            <Field
              label="Truck ID"
              required
              error={errors.truck_id}
              help="A new truck ID is also registered in the fleet."
            >
              <input
                id="config-truck-id"
                type="text"
                value={values.truck_id}
                onChange={(e) => set("truck_id", e.target.value)}
                disabled={lockTruckId}
                placeholder="e.g. TNK-001"
                className={`${INPUT_CLASS} disabled:bg-slate-50 disabled:text-slate-500`}
              />
            </Field>
            <div className="col-span-2 space-y-3">
              {values.rows.map((row, idx) => {
                const extra = row.allowed_grades.filter(
                  (g) => !(PRODUCT_CODES as string[]).includes(g),
                );
                const rowLabel = `Compartment ${idx + 1}`;
                return (
                  <fieldset
                    key={idx}
                    className="rounded-lg border border-slate-200 p-3"
                  >
                    <legend className="px-1 text-xs font-semibold text-slate-700">
                      {rowLabel}
                    </legend>
                    <div className="grid grid-cols-[1fr_1fr_88px_auto] items-start gap-2">
                      <Field
                        label="Compartment ID"
                        error={errors[`rows.${idx}.compartment_id`]}
                        span={1}
                        id={`cmp-${idx}-id`}
                      >
                        <input
                          id={`cmp-${idx}-id`}
                          type="text"
                          value={row.compartment_id}
                          onChange={(e) =>
                            setRow(idx, { compartment_id: e.target.value })
                          }
                          placeholder="C1"
                          className={INPUT_CLASS}
                        />
                      </Field>
                      <Field
                        label="Capacity"
                        error={errors[`rows.${idx}.capacity_gallons`]}
                        span={1}
                        id={`cmp-${idx}-cap`}
                      >
                        <NumberField
                          id={`cmp-${idx}-cap`}
                          unit="gal"
                          min={1}
                          value={row.capacity_gallons}
                          onChange={(n) => setRow(idx, { capacity_gallons: n })}
                          placeholder="3,000"
                        />
                      </Field>
                      <Field label="Position" span={1} id={`cmp-${idx}-pos`}>
                        <NumberField
                          id={`cmp-${idx}-pos`}
                          min={0}
                          value={row.position_index}
                          onChange={(n) => setRow(idx, { position_index: n })}
                        />
                      </Field>
                      <div className="pt-5">
                        {values.rows.length > 1 && (
                          <button
                            type="button"
                            onClick={() =>
                              set(
                                "rows",
                                values.rows.filter((_, i) => i !== idx),
                              )
                            }
                            className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 hover:bg-red-50 hover:text-red-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                            aria-label={`Remove compartment ${idx + 1}`}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        )}
                      </div>
                    </div>
                    <div className="mt-2">
                      <p
                        id={`cmp-${idx}-grades`}
                        className="mb-1 text-xs font-medium text-slate-700"
                      >
                        Allowed products
                      </p>
                      <div
                        role="group"
                        aria-labelledby={`cmp-${idx}-grades`}
                        className="flex flex-wrap gap-1.5"
                      >
                        {[...(PRODUCT_CODES as string[]), ...extra].map(
                          (code) => {
                            const on = row.allowed_grades.includes(code);
                            return (
                              <button
                                key={code}
                                type="button"
                                aria-pressed={on}
                                title={code}
                                onClick={() =>
                                  setRow(idx, {
                                    allowed_grades: on
                                      ? row.allowed_grades.filter(
                                          (g) => g !== code,
                                        )
                                      : [...row.allowed_grades, code],
                                  })
                                }
                                className={`inline-flex h-7 items-center gap-1.5 rounded-full border px-2 text-xs font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
                                  on
                                    ? "border-primary bg-primary-soft text-brand-800"
                                    : "border-slate-300 bg-surface text-slate-700 hover:bg-slate-50"
                                }`}
                              >
                                {on ? (
                                  <Check
                                    aria-hidden="true"
                                    className="h-3 w-3"
                                  />
                                ) : (
                                  <ProductCap code={code} decorative />
                                )}
                                {productName(code)}
                              </button>
                            );
                          },
                        )}
                      </div>
                      {errors[`rows.${idx}.allowed_grades`] && (
                        <p className="mt-1 text-xs text-red-700">
                          {errors[`rows.${idx}.allowed_grades`]}
                        </p>
                      )}
                    </div>
                  </fieldset>
                );
              })}
              <Button
                type="button"
                variant="secondary"
                size="sm"
                icon={<Plus className="h-3.5 w-3.5" />}
                onClick={() =>
                  set("rows", [...values.rows, emptyRow(values.rows.length)])
                }
              >
                Add compartment
              </Button>
            </div>
          </>
        );
      }}
    </FormDialog>
  );
}
