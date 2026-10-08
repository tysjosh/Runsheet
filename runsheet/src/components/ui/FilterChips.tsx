"use client";

/**
 * FilterChips: status filters with their counts inside the chip (R4.4). They
 * replace KPI card bands above tables. A `role="group"` of toggle buttons
 * (`aria-pressed`), not a tablist, because they filter the same list.
 */
import type { StatusKey } from "../../styles/tokens";
import { STATUS } from "../../styles/tokens";

export interface FilterChipOption {
  id: string;
  label: string;
  count?: number;
  /** Adds the status dot (with the label, so colour is not the only signal). */
  status?: StatusKey;
}

export interface FilterChipsProps {
  options: FilterChipOption[];
  /** Selected id (single) or ids (multi). */
  value: string | string[];
  onChange: (value: string | string[]) => void;
  multi?: boolean;
  label: string;
  className?: string;
}

export function FilterChips({
  options,
  value,
  onChange,
  multi = false,
  label,
  className = "",
}: FilterChipsProps) {
  const selected = new Set(Array.isArray(value) ? value : [value]);
  const toggle = (id: string) => {
    if (!multi) {
      onChange(id);
      return;
    }
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange([...next]);
  };
  return (
    <div
      role="group"
      aria-label={label}
      className={`flex min-w-0 items-center gap-1.5 ${className}`}
    >
      {options.map((o) => {
        const on = selected.has(o.id);
        const dot = o.status ? STATUS[o.status].dot : undefined;
        return (
          <button
            key={o.id}
            type="button"
            aria-pressed={on}
            onClick={() => toggle(o.id)}
            className={`inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs font-semibold transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
              on
                ? "border-primary bg-primary-soft text-brand-800"
                : "border-slate-300 bg-surface text-slate-700 hover:bg-slate-50"
            }`}
          >
            {dot && (
              <span
                aria-hidden="true"
                className="h-2 w-2 rounded-full"
                style={{ backgroundColor: dot }}
              />
            )}
            {o.label}
            {o.count !== undefined && (
              <span
                className={`rounded-full px-1.5 tabular-nums ${on ? "bg-white/70" : "bg-slate-100"}`}
              >
                {o.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
