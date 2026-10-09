"use client";

/**
 * FilterPopover: the toolbar's "Filters · n" chip (design.md §6 rule 2). The
 * secondary filters live in a popover, so they take 0 px until opened.
 * Escape and an outside click close it; focus returns to the chip.
 */
import { Filter } from "lucide-react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

export interface FilterPopoverProps {
  /** Active secondary filters (shown as "Filters · n"). */
  count: number;
  children: ReactNode;
  /** Accessible name of the panel. */
  label?: string;
  onClear?: () => void;
  className?: string;
}

export function FilterPopover({
  count,
  children,
  label = "Filters",
  onClear,
  className = "",
}: FilterPopoverProps) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  return (
    <div
      ref={wrapRef}
      className={`relative shrink-0 ${className}`}
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.preventDefault();
          e.stopPropagation();
          setOpen(false);
          buttonRef.current?.focus();
        }
      }}
    >
      <button
        ref={buttonRef}
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((o) => !o)}
        className={`inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs font-semibold focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
          count > 0
            ? "border-primary bg-primary-soft text-brand-800"
            : "border-slate-300 bg-surface text-slate-800 hover:bg-slate-50"
        }`}
      >
        <Filter aria-hidden="true" className="h-3.5 w-3.5" />
        {count > 0 ? `Filters · ${count}` : "Filters"}
      </button>
      {open && (
        <div
          id={panelId}
          role="group"
          aria-label={label}
          className="absolute left-0 top-full z-40 mt-1 w-max min-w-64 max-w-[min(44rem,85vw)] rounded-lg border border-slate-200 bg-surface p-3 shadow-lg"
        >
          {children}
          {onClear && count > 0 && (
            <div className="mt-3 flex justify-end border-t border-slate-100 pt-2">
              <button
                type="button"
                onClick={onClear}
                className="rounded text-xs font-semibold text-link hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
              >
                Clear filters
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default FilterPopover;
