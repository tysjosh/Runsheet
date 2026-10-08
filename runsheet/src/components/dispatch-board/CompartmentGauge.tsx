/**
 * Compartment gauge (design K14.7, R5.3, R5.4, R19.4). One segment per
 * compartment in position order, width proportional to capacity, fill height
 * = planned volume / capacity. Each segment shows its product code and fill
 * as text; `needs_cleaning` adds a hatch plus the word "Clean"; a compartment
 * a hovered drop would block gets a red outline plus "Blocked". The list
 * carries the text alternative "Compartment 2, ULSD, 92% full".
 *
 * During a drag, `preview` (the server's `fill_by_compartment`, in percent)
 * replaces the current fill so the dispatcher sees the post-drop state.
 */

import type {
  Allocation,
  Check,
  CompartmentView,
} from "../../services/dispatchBoardApi";
import { PRODUCT } from "../../styles/tokens";
import { productToken } from "../ui/ProductChip";

export interface GaugePreview {
  /** Percent full per compartment after the drop. */
  fill: Record<string, number>;
  /** Compartments the drop would block. */
  blocked: string[];
  /** Compartments the drop would need cleaned first. */
  cleaning: string[];
}

export interface CompartmentGaugeProps {
  compartments: CompartmentView[];
  allocations: Allocation[];
  preview?: GaugePreview | null;
  compact?: boolean;
  /** Visible label, e.g. "Load 1". */
  label?: string;
}

export interface SegmentFacts {
  compartmentId: string;
  number: number;
  product: string | null;
  percent: number;
  needsCleaning: boolean;
  blocked: boolean;
}

/** The facts each segment shows; exported for tests and announcements. */
export function gaugeSegments(
  compartments: CompartmentView[],
  allocations: Allocation[],
  preview?: GaugePreview | null,
): SegmentFacts[] {
  return [...compartments]
    .sort((a, b) => a.position_index - b.position_index)
    .map((c) => {
      const rows = allocations.filter(
        (a) => a.compartment_id === c.compartment_id,
      );
      const liters = rows.reduce((s, a) => s + a.liters, 0);
      const current =
        c.capacity_l > 0 ? Math.round((liters / c.capacity_l) * 100) : 0;
      const previewed = preview?.fill[c.compartment_id];
      const percent =
        preview && previewed !== undefined
          ? Math.round(previewed)
          : preview
            ? 0
            : current;
      return {
        compartmentId: c.compartment_id,
        number: c.position_index + 1,
        product:
          rows.find((r) => r.product_code)?.product_code ??
          (percent === 0 ? null : c.last_loaded_product),
        percent: Math.max(0, Math.min(100, percent)),
        needsCleaning:
          c.state === "needs_cleaning" ||
          (preview?.cleaning.includes(c.compartment_id) ?? false),
        blocked: preview?.blocked.includes(c.compartment_id) ?? false,
      };
    });
}

export function segmentText(s: SegmentFacts): string {
  const parts = [
    `Compartment ${s.number}`,
    s.product ?? "empty",
    `${s.percent}% full`,
  ];
  if (s.needsCleaning) parts.push("needs cleaning");
  if (s.blocked) parts.push("blocked by this change");
  return parts.join(", ");
}

/** Compartments named by blocking / cleaning checks (fix links, K3.2). */
export function previewFromChecks(
  fill: Record<string, number>,
  checks: Check[],
): GaugePreview {
  const blocked: string[] = [];
  const cleaning: string[] = [];
  for (const c of checks) {
    const id = c.fix_link?.kind === "compartment" ? c.fix_link.id : null;
    if (!id) continue;
    if (c.outcome === "block") blocked.push(id);
    else if (c.reason_code === "requires_cleaning") cleaning.push(id);
  }
  return { fill, blocked, cleaning };
}

/** Short visible product text: the RP 1637 symbol for catalog codes. */
function gaugeProductText(code: string | null): string {
  if (!code) return "—";
  // Unknown codes show as sent (the `_unknown` rule of ProductChip).
  return code in PRODUCT ? productToken(code).symbol : code;
}

const HATCH =
  "repeating-linear-gradient(45deg, transparent 0 3px, rgba(0,0,0,0.18) 3px 5px)";

export function CompartmentGauge({
  compartments,
  allocations,
  preview,
  compact = false,
  label,
}: CompartmentGaugeProps) {
  const segments = gaugeSegments(compartments, allocations, preview);
  if (segments.length === 0) {
    return <span className="text-xs text-gray-500">No compartments</span>;
  }
  const total = compartments.reduce((s, c) => s + c.capacity_l, 0) || 1;
  const caps = Object.fromEntries(
    compartments.map((c) => [c.compartment_id, c.capacity_l]),
  );
  return (
    <div className="min-w-0" data-testid="compartment-gauge">
      {label && (
        <span className="mb-0.5 block text-[11px] text-gray-500">{label}</span>
      )}
      <ul
        aria-label={`${label ? `${label} ` : ""}compartments${preview ? " after this change" : ""}`}
        className={`flex w-full gap-0.5 ${compact ? "h-4" : "h-7"}`}
      >
        {segments.map((s) => (
          <li
            key={s.compartmentId}
            data-compartment={s.compartmentId}
            data-blocked={s.blocked || undefined}
            data-product={s.product ?? undefined}
            className={`relative overflow-hidden rounded-sm border bg-slate-100 ${
              s.blocked ? "border-2 border-red-700" : "border-slate-300"
            }`}
            style={{
              width: `${((caps[s.compartmentId] ?? 0) / total) * 100}%`,
              backgroundImage: s.needsCleaning ? HATCH : undefined,
            }}
            title={segmentText(s)}
          >
            {/* Fill in the product's RP 1637 colour; the label names it. */}
            <span
              aria-hidden="true"
              className="absolute inset-x-0 bottom-0 border-t"
              style={{
                height: `${s.percent}%`,
                backgroundColor: s.product
                  ? productToken(s.product).bg
                  : "var(--rs-slate-300, #cbd5e1)",
                borderColor: s.product
                  ? productToken(s.product).border
                  : "transparent",
              }}
            />
            <span className="sr-only">{segmentText(s)}</span>
            <span
              aria-hidden="true"
              className="relative m-px inline-block max-w-[calc(100%-2px)] truncate rounded-sm bg-white/90 px-0.5 text-[10px] font-semibold leading-tight text-slate-900"
            >
              {compact
                ? `${s.percent}%`
                : `${gaugeProductText(s.product)} ${s.percent}%`}
              {s.needsCleaning && " Clean"}
              {s.blocked && " Blocked"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default CompartmentGauge;
