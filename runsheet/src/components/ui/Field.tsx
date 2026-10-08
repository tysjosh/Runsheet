"use client";

/**
 * Field: label, help and error around one control (R6.3).
 *
 * The control gets `id`, `aria-describedby` (help + error), `aria-invalid`
 * and `aria-required` injected, so callers never wire ids by hand. Inside a
 * `FormGrid` (FormDialog's body) `span={1}` puts two short fields side by
 * side; the default spans the full row.
 */
import {
  cloneElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
  useId,
} from "react";

export interface FieldProps {
  label: ReactNode;
  help?: ReactNode;
  error?: string | null;
  required?: boolean;
  /** 1 = half row, 2 = full row (default). */
  span?: 1 | 2;
  /** Explicit control id (otherwise generated). */
  id?: string;
  children: ReactElement<Record<string, unknown>>;
  className?: string;
}

export function Field({
  label,
  help,
  error,
  required = false,
  span = 2,
  id,
  children,
  className = "",
}: FieldProps) {
  const auto = useId();
  const controlId =
    id ?? (children.props.id as string | undefined) ?? `field-${auto}`;
  const helpId = help ? `${controlId}-help` : undefined;
  const errorId = error ? `${controlId}-error` : undefined;
  const describedBy =
    [children.props["aria-describedby"] as string | undefined, helpId, errorId]
      .filter(Boolean)
      .join(" ") || undefined;

  const control = isValidElement(children)
    ? cloneElement(children, {
        id: controlId,
        "aria-describedby": describedBy,
        "aria-invalid": error ? true : undefined,
        "aria-required": required || undefined,
      })
    : children;

  return (
    <div
      className={`flex min-w-0 flex-col gap-1 ${span === 2 ? "col-span-2" : "col-span-2 sm:col-span-1"} ${className}`}
    >
      <label htmlFor={controlId} className="text-sm font-medium text-text">
        {label}
        {required && (
          <span className="ml-0.5 text-red-700" aria-hidden="true">
            *
          </span>
        )}
      </label>
      {control}
      {help && !error && (
        <p id={helpId} className="text-xs text-text-muted">
          {help}
        </p>
      )}
      {help && error && (
        <p id={helpId} className="sr-only">
          {help}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-xs font-medium text-red-700">
          {error}
        </p>
      )}
    </div>
  );
}

/** Two-column grid for Field rows. */
export function FormGrid({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`grid grid-cols-2 gap-x-4 gap-y-3 ${className}`}>
      {children}
    </div>
  );
}

/** Shared input classes so every control in a form looks the same. */
export const INPUT_CLASS =
  "h-8 w-full min-w-0 rounded-lg border border-slate-300 bg-surface px-2.5 text-sm text-text placeholder:text-slate-500 focus:border-primary focus:outline-none focus:ring-2 focus:ring-focus/30 disabled:cursor-not-allowed disabled:bg-slate-100 aria-[invalid=true]:border-red-600";
