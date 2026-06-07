/**
 * SearchableSelect — accessible, searchable single-select dropdown.
 *
 * Replaces free-text "type the ID from memory" inputs (assign driver, replan
 * entity, etc.) with a filterable list of real options. The host supplies the
 * options (typically loaded from a list endpoint); this component owns the
 * open/close, search filtering, keyboard navigation, and outside-click
 * dismissal.
 *
 * Keyboard: type to filter, ArrowUp/Down to move, Enter to select, Escape to
 * close.
 */

"use client";

import { Check, ChevronDown, Search, X } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

export interface SearchableSelectOption {
  value: string;
  label: string;
  /** Secondary line shown under the label (e.g. CDL class, asset type). */
  sublabel?: string;
  disabled?: boolean;
}

export interface SearchableSelectProps {
  options: SearchableSelectOption[];
  value: string | null;
  onChange: (value: string) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  loading?: boolean;
  disabled?: boolean;
  /** When true, shows a clear (×) control that resets the value to "". */
  allowClear?: boolean;
  emptyMessage?: string;
  className?: string;
  "aria-label"?: string;
  id?: string;
}

export function SearchableSelect({
  options,
  value,
  onChange,
  placeholder = "Select…",
  searchPlaceholder = "Search…",
  loading = false,
  disabled = false,
  allowClear = false,
  emptyMessage = "No matches found",
  className = "",
  "aria-label": ariaLabel,
  id,
}: SearchableSelectProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listboxId = useId();

  const selected = options.find((o) => o.value === value) ?? null;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) =>
      `${o.label} ${o.sublabel ?? ""} ${o.value}`.toLowerCase().includes(q),
    );
  }, [options, query]);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  // Focus the search field and reset highlight whenever the panel opens.
  useEffect(() => {
    if (open) {
      setQuery("");
      setHighlight(0);
      // Defer so the input is mounted before focusing.
      const t = setTimeout(() => searchRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
  }, [open]);

  const commit = (option: SearchableSelectOption) => {
    if (option.disabled) return;
    onChange(option.value);
    setOpen(false);
  };

  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const option = filtered[highlight];
      if (option) commit(option);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    }
  };

  return (
    <div ref={containerRef} className={`relative ${className}`}>
      <button
        type="button"
        id={id}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        className="flex w-full items-center justify-between gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2 text-left text-sm focus:border-gray-300 focus:outline-none focus:ring-2 focus:ring-gray-200 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <span
          className={`truncate ${selected ? "text-gray-900" : "text-gray-400"}`}
        >
          {selected ? selected.label : placeholder}
        </span>
        <ChevronDown
          className={`h-4 w-4 flex-shrink-0 text-gray-400 transition-transform ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>

      {allowClear && value && !disabled && (
        <button
          type="button"
          onClick={() => onChange("")}
          aria-label="Clear selection"
          className="absolute right-8 top-1/2 -translate-y-1/2 rounded p-0.5 text-gray-400 hover:text-gray-600"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}

      {open && (
        <div className="absolute z-50 mt-1 w-full overflow-hidden rounded-lg border border-gray-200 bg-white shadow-lg">
          <div className="relative border-b border-gray-100 p-2">
            <Search className="absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
            <input
              ref={searchRef}
              type="text"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setHighlight(0);
              }}
              onKeyDown={handleSearchKeyDown}
              placeholder={searchPlaceholder}
              aria-label={searchPlaceholder}
              aria-controls={listboxId}
              className="w-full rounded-md border-0 bg-transparent py-1.5 pl-7 pr-2 text-sm focus:outline-none"
            />
          </div>
          <div
            id={listboxId}
            role="listbox"
            className="max-h-60 overflow-y-auto py-1"
          >
            {loading ? (
              <div className="px-3 py-3 text-center text-sm text-gray-400">
                Loading…
              </div>
            ) : filtered.length === 0 ? (
              <div className="px-3 py-3 text-center text-sm text-gray-400">
                {emptyMessage}
              </div>
            ) : (
              filtered.map((option, index) => {
                const isSelected = option.value === value;
                const isHighlighted = index === highlight;
                return (
                  <button
                    key={option.value}
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    disabled={option.disabled}
                    onClick={() => commit(option)}
                    onMouseEnter={() => setHighlight(index)}
                    className={`flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                      isHighlighted ? "bg-gray-100" : "bg-white"
                    }`}
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-gray-900">
                        {option.label}
                      </span>
                      {option.sublabel && (
                        <span className="block truncate text-xs text-gray-500">
                          {option.sublabel}
                        </span>
                      )}
                    </span>
                    {isSelected && (
                      <Check className="h-4 w-4 flex-shrink-0 text-primary" />
                    )}
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
