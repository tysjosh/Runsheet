/**
 * Select: a styled native <select>. Label it with `Field` (or pass
 * `aria-label` when it stands alone in a toolbar).
 */
import React from "react";
import { INPUT_CLASS } from "./Field";

export interface SelectOption {
  value: string;
  label: string;
  description?: string;
  disabled?: boolean;
}

export interface SelectProps
  extends Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "onChange"> {
  options: SelectOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}

export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  (
    { options, value, onChange, placeholder, className = "", ...props },
    ref,
  ) => (
    <select
      ref={ref}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`${INPUT_CLASS} pr-8 ${className}`}
      {...props}
    >
      {placeholder !== undefined && <option value="">{placeholder}</option>}
      {options.map((o) => (
        <option
          key={o.value}
          value={o.value}
          disabled={o.disabled}
          title={o.description}
        >
          {o.label}
        </option>
      ))}
    </select>
  ),
);
Select.displayName = "Select";
