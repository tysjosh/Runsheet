"use client";

/**
 * ProductSelect: choose a fuel product by its RP 1637 cap and readable name,
 * with the catalog code as secondary text (R6.5). Replaces `CODE (Name)`
 * option labels that truncated to "GASOLINE_REG (Regular U…".
 *
 * WAI-ARIA select-only combobox pattern: a button trigger (labelled by
 * `Field`) opens a listbox; arrows move, Enter/Space choose, Escape closes,
 * typing a letter jumps to the next matching name. The control's min-width
 * fits the longest label, and each label has a title tooltip on overflow.
 */
import { Check, ChevronDown } from "lucide-react";
import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import { productName } from "../../lib/format";
import { PRODUCT_CODES } from "../../styles/tokens";
import { ProductCap } from "./ProductChip";

export interface ProductSelectProps {
  value: string | null;
  onChange: (code: string) => void;
  /** Codes offered (defaults to the whole catalog, in catalog order). */
  options?: string[];
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  name?: string;
  className?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
  "aria-required"?: boolean;
  "aria-label"?: string;
}

export const ProductSelect = React.forwardRef<
  HTMLButtonElement,
  ProductSelectProps
>(
  (
    {
      value,
      onChange,
      options = PRODUCT_CODES,
      placeholder = "Select a product",
      disabled = false,
      id,
      name,
      className = "",
      ...aria
    },
    ref,
  ) => {
    const auto = useId();
    const listId = `${id ?? auto}-listbox`;
    const [open, setOpen] = useState(false);
    const [active, setActive] = useState(0);
    const wrapRef = useRef<HTMLDivElement>(null);
    const listRef = useRef<HTMLDivElement>(null);
    const innerRef = useRef<HTMLButtonElement | null>(null);

    const labels = useMemo(
      () => options.map((code) => ({ code, name: productName(code) })),
      [options],
    );
    // Room for "cap + longest name + code + chevron" without truncating.
    const minCh = Math.max(
      ...labels.map((l) => l.name.length + l.code.length + 7),
      22,
    );

    useEffect(() => {
      if (!open) return;
      const onDoc = (e: MouseEvent) => {
        if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
      };
      document.addEventListener("mousedown", onDoc);
      return () => document.removeEventListener("mousedown", onDoc);
    }, [open]);

    useEffect(() => {
      if (!open) return;
      listRef.current
        ?.querySelector<HTMLElement>(`[data-index="${active}"]`)
        ?.scrollIntoView?.({ block: "nearest" });
    }, [open, active]);

    const openList = () => {
      const i = Math.max(
        0,
        labels.findIndex((l) => l.code === value),
      );
      setActive(i);
      setOpen(true);
    };
    const choose = (i: number) => {
      const l = labels[i];
      if (!l) return;
      onChange(l.code);
      setOpen(false);
      innerRef.current?.focus();
    };

    const onKeyDown = (e: React.KeyboardEvent) => {
      if (disabled) return;
      if (!open) {
        if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
          e.preventDefault();
          openList();
        }
        return;
      }
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          setActive((a) => Math.min(labels.length - 1, a + 1));
          break;
        case "ArrowUp":
          e.preventDefault();
          setActive((a) => Math.max(0, a - 1));
          break;
        case "Home":
          e.preventDefault();
          setActive(0);
          break;
        case "End":
          e.preventDefault();
          setActive(labels.length - 1);
          break;
        case "Enter":
        case " ":
          e.preventDefault();
          choose(active);
          break;
        case "Escape":
          e.preventDefault();
          e.stopPropagation();
          setOpen(false);
          break;
        case "Tab":
          setOpen(false);
          break;
        default:
          if (e.key.length === 1) {
            const k = e.key.toLowerCase();
            const order = [
              ...labels.slice(active + 1),
              ...labels.slice(0, active + 1),
            ];
            const hit = order.find((l) => l.name.toLowerCase().startsWith(k));
            if (hit) setActive(labels.indexOf(hit));
          }
      }
    };

    const selected = labels.find((l) => l.code === value);

    return (
      <div
        ref={wrapRef}
        className={`relative ${className}`}
        style={{ minWidth: `${minCh}ch` }}
      >
        <button
          ref={(el) => {
            innerRef.current = el;
            if (typeof ref === "function") ref(el);
            else if (ref) ref.current = el;
          }}
          id={id}
          name={name}
          type="button"
          role="combobox"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={listId}
          aria-activedescendant={open ? `${listId}-${active}` : undefined}
          disabled={disabled}
          onClick={() => (open ? setOpen(false) : openList())}
          onKeyDown={onKeyDown}
          title={selected ? `${selected.name} (${selected.code})` : undefined}
          className="flex h-8 w-full items-center gap-2 rounded-lg border border-slate-300 bg-surface pl-2 pr-2 text-left text-sm text-text focus:border-primary focus:outline-none focus:ring-2 focus:ring-focus/30 disabled:cursor-not-allowed disabled:bg-slate-100 aria-[invalid=true]:border-red-600"
          {...aria}
        >
          {selected ? (
            <>
              <ProductCap code={selected.code} decorative />
              <span className="font-medium">{selected.name}</span>
              <span className="ml-auto font-mono text-[11px] text-text-muted">
                {selected.code}
              </span>
            </>
          ) : (
            <span className="text-slate-500">{placeholder}</span>
          )}
          <ChevronDown
            aria-hidden="true"
            className={`h-4 w-4 shrink-0 text-slate-500 ${selected ? "" : "ml-auto"}`}
          />
        </button>
        {open && (
          <div
            ref={listRef}
            id={listId}
            role="listbox"
            aria-label={aria["aria-label"] ?? "Products"}
            tabIndex={-1}
            className="absolute left-0 right-0 top-full z-[60] mt-1 max-h-72 overflow-y-auto rounded-lg border border-slate-200 bg-surface py-1 shadow-lg"
          >
            {labels.map((l, i) => {
              const isSel = l.code === value;
              return (
                <div
                  key={l.code}
                  id={`${listId}-${i}`}
                  role="option"
                  tabIndex={-1}
                  aria-selected={isSel}
                  data-index={i}
                  title={`${l.name} (${l.code})`}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => choose(i)}
                  onMouseEnter={() => setActive(i)}
                  className={`flex h-9 cursor-pointer items-center gap-2 px-2 text-sm ${
                    i === active ? "bg-slate-100" : ""
                  }`}
                >
                  <ProductCap code={l.code} decorative />
                  <span className="font-medium text-text">{l.name}</span>
                  <span className="ml-auto font-mono text-[11px] text-text-muted">
                    {l.code}
                  </span>
                  <Check
                    aria-hidden="true"
                    className={`h-4 w-4 shrink-0 text-primary ${isSel ? "" : "invisible"}`}
                  />
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  },
);
ProductSelect.displayName = "ProductSelect";
