"use client";

/**
 * FormDialog: the one create/edit pattern (R6, design.md §5), modelled on the
 * Edit Fuel Station modal the owner chose: centred, title with ×, a divider,
 * a labelled single-column form with two-column rows for short paired fields,
 * Cancel as a text button and a filled brand primary.
 *
 * Built on `Modal` (focus trap, Escape, focus return are Modal's), it adds:
 *
 * - sizes sm 400 / md 560 / lg 760 px; sticky header and footer, scrolling body
 * - dirty tracking: Esc, ×, backdrop or Cancel on a changed form asks
 *   "Discard changes?" (Keep editing / Discard)
 * - Enter submits from any field except a textarea or a control that handles
 *   Enter itself
 * - `validate(values)` before submit; per-step `validate` for `steps`
 * - error envelope mapping: `details.fields` ({field: message}) or
 *   `details.errors` ([{loc: [..., field], msg}], the FastAPI 422 shape) become
 *   inline field errors; otherwise the envelope `message` shows in a banner
 * - a spinner on the disabled primary while saving, a success toast, `onSaved`
 *
 * Children are a render prop: `({ values, set, errors }) => <Field …/>`.
 */
import {
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { Button } from "./Button";
import { FormGrid } from "./Field";
import { InlineBanner } from "./InlineBanner";
import { Modal } from "./Modal";
import { notify } from "./toast/notify";

export type FieldErrors = Record<string, string | undefined>;

export interface FormDialogStep<V> {
  id: string;
  title: string;
  validate?: (values: V) => FieldErrors;
}

export interface FormDialogSection {
  id: string;
  title: string;
}

export interface FormRenderProps<V> {
  values: V;
  set: <K extends keyof V>(field: K, value: V[K]) => void;
  setValues: (next: V | ((prev: V) => V)) => void;
  errors: FieldErrors;
  /** Current step id when `steps` are used. */
  step?: string;
  saving: boolean;
}

export interface FormDialogProps<
  V extends Record<string, unknown>,
  R = unknown,
> {
  open: boolean;
  title: string;
  /** Help text under the title; becomes the dialog's aria-describedby. */
  help?: ReactNode;
  size?: "sm" | "md" | "lg";
  submitLabel?: string;
  initialValues: V;
  validate?: (values: V) => FieldErrors;
  /** Throw (ideally an ApiError) to surface envelope errors. */
  onSubmit: (values: V) => Promise<R> | R;
  onClose: () => void;
  onSaved?: (result: R) => void;
  /** Toast after a successful save; `null` for none. */
  successMessage?: string | null;
  sections?: FormDialogSection[];
  steps?: FormDialogStep<V>[];
  children: (props: FormRenderProps<V>) => ReactNode;
  /** `sheet`: a bottom sheet below 640 px (see Modal). Additive, task 3.11. */
  mobile?: "sheet";
  /**
   * The submit stays focusable and announced but does nothing
   * (`aria-disabled`), e.g. while a precondition is unmet. Additive.
   */
  submitDisabled?: boolean;
  /** Stay open after a successful submit (the caller shows the result). */
  keepOpenOnSuccess?: boolean;
}

const WIDTH = { sm: "max-w-[400px]", md: "max-w-[560px]", lg: "max-w-[760px]" };

/** Shallow equality over the union of keys (dirty tracking). */
export function shallowEqual(
  a: Record<string, unknown>,
  b: Record<string, unknown>,
) {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of keys) {
    const x = a[k];
    const y = b[k];
    if (Object.is(x, y)) continue;
    if (Array.isArray(x) && Array.isArray(y)) {
      if (x.length !== y.length || x.some((v, i) => !Object.is(v, y[i])))
        return false;
      continue;
    }
    return false;
  }
  return true;
}

/**
 * Field errors from an error envelope. Accepts `details.fields` as an object
 * and `details.errors` as a FastAPI-style list. Returns null when neither
 * names a field.
 */
/**
 * A NumberField that holds text which isn't a number reports NaN. NaN is
 * never a valid value, so block the submit with a field error even when the
 * dialog's own `validate` doesn't check for it (additive, task 3.4).
 */
function withNotANumberErrors<V extends Record<string, unknown>>(
  values: V,
  errors: FieldErrors,
): FieldErrors {
  const out: FieldErrors = { ...errors };
  for (const [k, val] of Object.entries(values)) {
    if (typeof val === "number" && Number.isNaN(val) && !out[k])
      out[k] = "Enter a number.";
  }
  return out;
}

export function envelopeFieldErrors(err: unknown): FieldErrors | null {
  const details = (err as { details?: unknown } | null)?.details;
  if (!details || typeof details !== "object") return null;
  const d = details as Record<string, unknown>;
  const out: FieldErrors = {};
  if (d.fields && typeof d.fields === "object" && !Array.isArray(d.fields)) {
    for (const [k, v] of Object.entries(d.fields as Record<string, unknown>)) {
      if (typeof v === "string") out[k] = v;
      else if (Array.isArray(v) && typeof v[0] === "string") out[k] = v[0];
    }
  }
  if (Array.isArray(d.errors)) {
    for (const e of d.errors) {
      if (!e || typeof e !== "object") continue;
      const item = e as { loc?: unknown; msg?: unknown; field?: unknown };
      const loc = Array.isArray(item.loc) ? item.loc : [];
      const last = loc.length ? loc[loc.length - 1] : item.field;
      const field =
        typeof last === "string"
          ? last
          : typeof last === "number"
            ? String(last)
            : null;
      if (field && typeof item.msg === "string" && !out[field])
        out[field] = item.msg;
    }
  }
  return Object.keys(out).length ? out : null;
}

const hasErrors = (e: FieldErrors) => Object.values(e).some(Boolean);

export function FormDialog<V extends Record<string, unknown>, R = unknown>({
  open,
  title,
  help,
  size = "md",
  submitLabel = "Save changes",
  initialValues,
  validate,
  onSubmit,
  onClose,
  onSaved,
  successMessage = "Changes saved",
  sections,
  steps,
  children,
  mobile,
  submitDisabled = false,
  keepOpenOnSuccess = false,
}: FormDialogProps<V, R>) {
  const formId = useId();
  const helpId = useId();
  const [values, setValuesState] = useState<V>(initialValues);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [stepIndex, setStepIndex] = useState(0);
  const keepRef = useRef<HTMLButtonElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Reset whenever the dialog opens.
  useEffect(() => {
    if (open) {
      setValuesState(initialValues);
      setErrors({});
      setFormError(null);
      setSaving(false);
      setConfirming(false);
      setStepIndex(0);
    }
  }, [open]);

  useEffect(() => {
    if (confirming) keepRef.current?.focus();
  }, [confirming]);

  // Start in the first field rather than on the × (Modal focuses the first
  // focusable, which is the close button). Runs after Modal's effect.
  useEffect(() => {
    if (!open) return;
    bodyRef.current
      ?.querySelector<HTMLElement>(
        'input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [role="combobox"]:not([disabled])',
      )
      ?.focus();
  }, [open, stepIndex]);

  const dirty = useMemo(
    () => !shallowEqual(values, initialValues),
    [values, initialValues],
  );

  // Stable close handler for Modal's focus-trap effect (a new function each
  // render would re-run it and yank focus back to the first field).
  const stateRef = useRef({ dirty, confirming, saving, onClose });
  stateRef.current = { dirty, confirming, saving, onClose };
  const requestClose = useCallback(() => {
    const s = stateRef.current;
    if (s.saving) return;
    if (s.confirming) {
      setConfirming(false);
      return;
    }
    if (s.dirty) {
      setConfirming(true);
      return;
    }
    s.onClose();
  }, []);

  const set = useCallback(<K extends keyof V>(field: K, value: V[K]) => {
    setValuesState((prev) => ({ ...prev, [field]: value }));
    setErrors((prev) =>
      prev[field as string] ? { ...prev, [field as string]: undefined } : prev,
    );
  }, []);

  const setValues = useCallback((next: V | ((prev: V) => V)) => {
    setValuesState((prev) =>
      typeof next === "function" ? (next as (p: V) => V)(prev) : next,
    );
  }, []);

  const isLastStep = !steps || stepIndex === steps.length - 1;

  const submit = async () => {
    if (saving || (submitDisabled && isLastStep)) return;
    setFormError(null);
    if (steps && !isLastStep) {
      const stepErrors = steps[stepIndex].validate?.(values) ?? {};
      if (hasErrors(stepErrors)) {
        setErrors(stepErrors);
        return;
      }
      setErrors({});
      setStepIndex((i) => i + 1);
      return;
    }
    const v = withNotANumberErrors(values, validate?.(values) ?? {});
    if (hasErrors(v)) {
      setErrors(v);
      focusFirstInvalid();
      return;
    }
    setSaving(true);
    try {
      const result = await onSubmit(values);
      setSaving(false);
      if (!keepOpenOnSuccess) onClose();
      if (successMessage) notify({ type: "success", message: successMessage });
      onSaved?.(result);
    } catch (err) {
      setSaving(false);
      const fieldErrors = envelopeFieldErrors(err);
      if (fieldErrors) {
        setErrors(fieldErrors);
        const msg = (err as Error)?.message;
        // Keep the envelope message as context when it adds something.
        setFormError(
          msg && !Object.values(fieldErrors).includes(msg) ? msg : null,
        );
        focusFirstInvalid();
      } else {
        const msg = err instanceof Error && err.message ? err.message : null;
        setFormError(msg ?? "Couldn't save. Try again.");
      }
    }
  };

  const focusFirstInvalid = () => {
    requestAnimationFrame(() => {
      bodyRef.current
        ?.querySelector<HTMLElement>('[aria-invalid="true"]')
        ?.focus();
    });
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLFormElement>) => {
    if (e.key !== "Enter" || e.defaultPrevented || e.shiftKey) return;
    const t = e.target as HTMLElement;
    const tag = t.tagName;
    if (tag === "TEXTAREA" || tag === "BUTTON" || tag === "A") return;
    if (t.getAttribute("role") === "combobox" || t.isContentEditable) return;
    e.preventDefault();
    void submit();
  };

  const stepper = steps ? (
    <ol
      aria-label="Steps"
      className="flex items-center gap-2 border-b border-gray-100 px-6 py-2 text-xs"
    >
      {steps.map((s, i) => (
        <li
          key={s.id}
          aria-current={i === stepIndex ? "step" : undefined}
          className={`flex items-center gap-1.5 font-medium ${
            i === stepIndex
              ? "text-brand-800"
              : i < stepIndex
                ? "text-slate-700"
                : "text-slate-500"
          }`}
        >
          <span
            className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-[11px] ${
              i <= stepIndex
                ? "bg-primary text-white"
                : "bg-slate-200 text-slate-700"
            }`}
          >
            {i + 1}
          </span>
          {s.title}
          {i < steps.length - 1 && (
            <span aria-hidden="true" className="mx-1 text-slate-400">
              ›
            </span>
          )}
        </li>
      ))}
    </ol>
  ) : sections && sections.length > 1 ? (
    <nav
      aria-label="Sections"
      className="flex gap-1.5 border-b border-gray-100 px-6 py-2"
    >
      {sections.map((s) => (
        <button
          key={s.id}
          type="button"
          onClick={() =>
            bodyRef.current
              ?.querySelector(`[data-section="${s.id}"]`)
              ?.scrollIntoView?.({ behavior: "smooth", block: "start" })
          }
          className="h-6 rounded-full border border-slate-300 px-2.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
        >
          {s.title}
        </button>
      ))}
    </nav>
  ) : null;

  const header = (
    <>
      {help && (
        <p id={helpId} className="px-6 pt-3 text-sm text-text-muted">
          {help}
        </p>
      )}
      {stepper}
    </>
  );

  const footer = (
    <>
      <Button variant="ghost" onClick={requestClose} disabled={saving}>
        Cancel
      </Button>
      {steps && stepIndex > 0 && (
        <Button
          variant="secondary"
          onClick={() => setStepIndex((i) => i - 1)}
          disabled={saving}
        >
          Back
        </Button>
      )}
      <Button
        type="submit"
        form={formId}
        variant="primary"
        loading={saving}
        aria-disabled={submitDisabled && isLastStep ? true : undefined}
        className={
          submitDisabled && isLastStep ? "cursor-not-allowed opacity-50" : ""
        }
      >
        {isLastStep ? submitLabel : "Next"}
      </Button>
    </>
  );

  const overlay = confirming ? (
    <div className="absolute inset-0 z-10 flex items-center justify-center rounded-xl bg-white/80">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={`${formId}-discard`}
        className="w-72 rounded-lg border border-slate-200 bg-white p-4 shadow-lg"
      >
        <p id={`${formId}-discard`} className="text-sm font-semibold text-text">
          Discard changes?
        </p>
        <p className="mt-1 text-xs text-text-muted">
          Your edits to this form will be lost.
        </p>
        <div className="mt-3 flex justify-end gap-2">
          <Button
            ref={keepRef}
            variant="secondary"
            size="sm"
            onClick={() => setConfirming(false)}
          >
            Keep editing
          </Button>
          <Button
            variant="danger"
            size="sm"
            onClick={() => {
              setConfirming(false);
              onClose();
            }}
          >
            Discard
          </Button>
        </div>
      </div>
    </div>
  ) : null;

  return (
    <Modal
      isOpen={open}
      onClose={requestClose}
      title={title}
      size="custom"
      closeLabel="Close"
      describedById={help ? helpId : undefined}
      className={`${WIDTH[size]} max-h-[calc(100vh-32px)]`}
      bodyClassName="min-h-0 flex-1"
      subheader={header}
      footer={footer}
      overlay={overlay}
      mobile={mobile}
    >
      <form
        id={formId}
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
        onKeyDown={onKeyDown}
        aria-busy={saving || undefined}
      >
        <div
          ref={bodyRef}
          className="max-h-[calc(100vh-160px)] overflow-y-auto px-6 py-4"
        >
          {formError && (
            <InlineBanner tone="critical" className="mb-3">
              {formError}
            </InlineBanner>
          )}
          <FormGrid>
            {children({
              values,
              set,
              setValues,
              errors,
              step: steps?.[stepIndex]?.id,
              saving,
            })}
          </FormGrid>
        </div>
      </form>
    </Modal>
  );
}

/** A titled group inside a sectioned FormDialog (spans the full row). */
export function FormSection({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <section
      data-section={id}
      aria-labelledby={`section-${id}`}
      className="col-span-2 grid grid-cols-2 gap-x-4 gap-y-3 border-t border-slate-100 pt-3 first:border-t-0 first:pt-0"
    >
      <h3
        id={`section-${id}`}
        className="col-span-2 text-sm font-semibold text-text"
      >
        {title}
      </h3>
      {children}
    </section>
  );
}
