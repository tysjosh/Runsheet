"use client";
/**
 * FilterChips: status filters with their counts inside the chip (R4.4). They
 * replace KPI card bands above tables. A `role="group"` of toggle buttons
 * (`aria-pressed`), not a tablist, because they filter the same list.
 *
 * `collapse`: the strip never clips or scrolls (R4.3). Chips that don't fit
 * move into a "More" menu in declared order, last option first; the selected
 * chip always stays visible. The strip asks for its full width and shrinks
 * with the toolbar, so it re-expands when the row grows.
 */
import { ChevronDown } from "lucide-react";
import {
  type ReactNode,
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { StatusKey } from "../../styles/tokens";
import { STATUS } from "../../styles/tokens";
import { Menu } from "./Menu";

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
  /** Move chips that don't fit into a "More" menu instead of clipping. */
  collapse?: boolean;
}

const GAP = 6; // gap-1.5

/** How many leading chips fit in `avail` px, leaving room for "More". */
export function fitChips(
  widths: number[],
  moreWidth: number,
  avail: number,
): number {
  const total = widths.reduce((s, w) => s + w, 0) + GAP * (widths.length - 1);
  if (total <= avail) return widths.length;
  let used = moreWidth;
  let n = 0;
  for (const w of widths) {
    if (used + GAP + w > avail) break;
    used += GAP + w;
    n += 1;
  }
  return n;
}

function Dot({ status }: { status?: StatusKey }) {
  if (!status) return null;
  return (
    <span
      aria-hidden="true"
      className="h-2 w-2 shrink-0 rounded-full"
      style={{ backgroundColor: STATUS[status].dot }}
    />
  );
}

export function FilterChips({
  options,
  value,
  onChange,
  multi = false,
  label,
  className = "",
  collapse = false,
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

  const rowRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  const [fullWidth, setFullWidth] = useState<number | undefined>(undefined);
  const [fit, setFit] = useState(options.length);

  const measure = useCallback(() => {
    const row = rowRef.current;
    const m = measureRef.current;
    if (!collapse || !row || !m) return;
    const kids = Array.from(m.children) as HTMLElement[];
    const widths = kids
      .slice(0, options.length)
      .map((k) => k.getBoundingClientRect().width);
    const moreWidth = kids[options.length]?.getBoundingClientRect().width ?? 0;
    const total =
      widths.reduce((s, w) => s + w, 0) + GAP * Math.max(0, widths.length - 1);
    // Nothing laid out (hidden, or no layout engine): show every chip.
    if (total === 0 || row.clientWidth === 0) {
      setFit(options.length);
      return;
    }
    setFullWidth(Math.ceil(total));
    setFit(fitChips(widths, moreWidth, row.clientWidth));
  }, [collapse, options.length]);

  // Re-measure when the labels or counts change, and on every resize.
  const signature = options.map((o) => `${o.id}:${o.count ?? ""}`).join("|");
  useLayoutEffect(() => {
    measure();
  }, [measure, signature]);
  useLayoutEffect(() => {
    const row = rowRef.current;
    if (!collapse || !row || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(row);
    return () => ro.disconnect();
  }, [collapse, measure]);

  // The visible set: the first `fit` chips, with the selected one swapped in.
  let shown = options;
  let hidden: FilterChipOption[] = [];
  if (collapse && fit < options.length) {
    const head = options.slice(0, fit);
    const sel = options.find((o) => selected.has(o.id) && !head.includes(o));
    if (sel && head.length > 0) head[head.length - 1] = sel;
    else if (sel) head.push(sel);
    const keep = new Set(head.map((o) => o.id));
    shown = options.filter((o) => keep.has(o.id));
    hidden = options.filter((o) => !keep.has(o.id));
  }

  const chip = (o: FilterChipOption, interactive: boolean): ReactNode => {
    const on = selected.has(o.id);
    return (
      <button
        key={o.id}
        type="button"
        aria-pressed={interactive ? on : undefined}
        tabIndex={interactive ? undefined : -1}
        onClick={interactive ? () => toggle(o.id) : undefined}
        className={`inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs font-semibold transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
          on
            ? "border-primary bg-primary-soft text-brand-800"
            : "border-slate-300 bg-surface text-slate-700 hover:bg-slate-50"
        }`}
      >
        <Dot status={o.status} />
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
  };
  const moreButton = (n: number, props?: object): ReactNode => (
    <button
      {...props}
      type="button"
      aria-label={`More ${label.toLowerCase()} filters (${n})`}
      className="inline-flex h-7 shrink-0 items-center gap-1 whitespace-nowrap rounded-full border border-slate-300 bg-surface px-2.5 text-xs font-semibold text-slate-700 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
    >
      More · {n}
      <ChevronDown aria-hidden="true" className="h-3 w-3" />
    </button>
  );

  return (
    <div
      ref={rowRef}
      role="group"
      aria-label={label}
      data-chip-strip={collapse ? "collapse" : undefined}
      style={collapse && fullWidth ? { width: fullWidth } : undefined}
      className={`relative flex min-w-0 items-center gap-1.5 ${collapse ? "shrink overflow-hidden" : ""} ${className}`}
    >
      {shown.map((o) => chip(o, true))}
      {hidden.length > 0 && (
        <Menu
          label={`More ${label.toLowerCase()} filters`}
          align="start"
          items={hidden.map((o) => ({
            id: o.id,
            label: o.count !== undefined ? `${o.label} (${o.count})` : o.label,
            icon: <Dot status={o.status} />,
            checked: selected.has(o.id),
            onSelect: () => toggle(o.id),
          }))}
          trigger={(p) => moreButton(hidden.length, p)}
        />
      )}
      {collapse && (
        // Off-screen copy of every chip, used only to measure widths.
        <div
          ref={measureRef}
          aria-hidden="true"
          inert
          className="pointer-events-none invisible absolute left-0 top-0 flex gap-1.5"
        >
          {options.map((o) => chip(o, false))}
          {moreButton(options.length)}
        </div>
      )}
    </div>
  );
}
