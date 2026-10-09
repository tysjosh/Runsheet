"use client";

/**
 * NumberField: locale-aware numeric input (R6.4).
 *
 * - `type="text" inputMode="decimal"`, so the browser never shows a raw float
 *   like `5283,441047162968` and the caret is not fought by `type=number`.
 * - Displays `format.number(value, { decimals })` when not focused (gallons
 *   are whole numbers unless the field declares decimals).
 * - Parses the locale's group and decimal separators ("5.283,4" in de-DE,
 *   "5,283.4" in en-US). Text that is not a number reports `NaN`, which
 *   `validateNumber` turns into "Enter a number".
 * - The unit renders as a suffix and is part of the accessible description.
 */
import React, { useEffect, useId, useState } from "react";
import { number as formatNumber, parseNumber } from "../../lib/format";
import { INPUT_CLASS } from "./Field";

export interface NumberRules {
  min?: number;
  max?: number;
  step?: number;
  decimals?: number;
  required?: boolean;
}

/** The validation message for a value under the rules, or null when valid. */
export function validateNumber(
  value: number | null | undefined,
  { min, max, step, decimals = 0, required = false }: NumberRules = {},
): string | null {
  if (value === null || value === undefined) {
    return required ? "Required" : null;
  }
  if (Number.isNaN(value)) return "Enter a number";
  if (min !== undefined && value < min) {
    return `Must be at least ${formatNumber(min, { decimals })}`;
  }
  if (max !== undefined && value > max) {
    return `Must be at most ${formatNumber(max, { decimals })}`;
  }
  if (step !== undefined && step > 0) {
    const base = min ?? 0;
    const k = (value - base) / step;
    if (Math.abs(k - Math.round(k)) > 1e-9) {
      return `Use steps of ${formatNumber(step, { decimals })}`;
    }
  }
  return null;
}

const round = (v: number, decimals: number) => {
  const f = 10 ** decimals;
  return Math.round(v * f) / f;
};

export interface NumberFieldProps
  extends Omit<
    React.InputHTMLAttributes<HTMLInputElement>,
    "value" | "onChange" | "type" | "min" | "max" | "step"
  > {
  value: number | null;
  onChange: (value: number | null) => void;
  unit?: "gal" | "%" | "$" | "L" | string;
  decimals?: number;
  min?: number;
  max?: number;
  step?: number;
  locale?: string;
}

export const NumberField = React.forwardRef<HTMLInputElement, NumberFieldProps>(
  (
    {
      value,
      onChange,
      unit,
      decimals = 0,
      min,
      max,
      step,
      locale,
      className = "",
      onFocus,
      onBlur,
      "aria-invalid": ariaInvalid,
      "aria-describedby": ariaDescribedBy,
      ...props
    },
    ref,
  ) => {
    const unitId = useId();
    const show = (v: number | null) =>
      v === null || Number.isNaN(v)
        ? ""
        : formatNumber(v, { decimals, locale });
    const [text, setText] = useState(() => show(value));
    const [focused, setFocused] = useState(false);

    // Follow external value changes while the user is not editing.
    useEffect(() => {
      if (!focused) setText(show(value));
    }, [value, focused, decimals, locale]);

    const invalid =
      validateNumber(value, { min, max, step, decimals }) !== null;
    const describedBy =
      [ariaDescribedBy, unit ? unitId : undefined].filter(Boolean).join(" ") ||
      undefined;

    return (
      <div className="relative min-w-0">
        <input
          ref={ref}
          type="text"
          inputMode={
            decimals > 0 || (min !== undefined && min < 0)
              ? "decimal"
              : "numeric"
          }
          autoComplete="off"
          value={text}
          {...props}
          aria-invalid={ariaInvalid || invalid || undefined}
          aria-describedby={describedBy}
          className={`${INPUT_CLASS} tabular-nums ${unit ? (unit === "$" ? "pl-6" : "pr-10") : ""} ${className}`}
          onFocus={(e) => {
            setFocused(true);
            onFocus?.(e);
          }}
          onBlur={(e) => {
            setFocused(false);
            setText(show(value));
            onBlur?.(e);
          }}
          onChange={(e) => {
            const next = e.target.value;
            setText(next);
            const parsed = parseNumber(next, { locale });
            if (parsed === null) onChange(null);
            else if (Number.isNaN(parsed)) onChange(Number.NaN);
            else onChange(round(parsed, decimals));
          }}
        />
        {unit && (
          <span
            id={unitId}
            className={`pointer-events-none absolute inset-y-0 flex items-center text-sm text-text-muted ${unit === "$" ? "left-2.5" : "right-2.5"}`}
          >
            {unit}
          </span>
        )}
      </div>
    );
  },
);
NumberField.displayName = "NumberField";
