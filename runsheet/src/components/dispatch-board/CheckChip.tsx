/**
 * Validation outcome chip (design K14.7, R9.1, R9.2, R19.4). Every outcome
 * carries an icon and text, so colour is never the only signal: pass = check
 * + "OK", warn = triangle + reason, block = octagon + reason, info = "i" +
 * reason. `more` adds "+n" for other reasons (R9.1).
 */
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  Loader2,
  OctagonAlert,
} from "lucide-react";
import type { CheckOutcome } from "../../services/dispatchBoardApi";

export type ChipOutcome = CheckOutcome | "checking";

// Status tokens (100 bg, 300 border, 800 text: all ≥ 4.5:1) + icon + text.
const STYLE: Record<ChipOutcome, string> = {
  pass: "border-brand-300 bg-brand-100 text-brand-800",
  warn: "border-amber-300 bg-amber-100 text-amber-800",
  block: "border-red-300 bg-red-100 text-red-800",
  info: "border-blue-300 bg-blue-50 text-blue-800",
  checking: "border-slate-300 bg-white text-slate-700",
};

const PREFIX: Record<ChipOutcome, string> = {
  pass: "OK",
  warn: "Warning",
  block: "Blocked",
  info: "Info",
  checking: "Checking…",
};

function Icon({ outcome }: { outcome: ChipOutcome }) {
  const cls = "h-3.5 w-3.5 shrink-0";
  switch (outcome) {
    case "pass":
      return <CheckCircle2 className={cls} aria-hidden="true" />;
    case "warn":
      return <AlertTriangle className={cls} aria-hidden="true" />;
    case "block":
      return <OctagonAlert className={cls} aria-hidden="true" />;
    case "checking":
      return (
        <Loader2
          className={`${cls} motion-safe:animate-spin`}
          aria-hidden="true"
        />
      );
    default:
      return <Info className={cls} aria-hidden="true" />;
  }
}

export interface CheckChipProps {
  outcome: ChipOutcome;
  /** Short reason; pass and checking need none. */
  label?: string | null;
  /** Number of other reasons not shown. */
  more?: number;
  className?: string;
}

/** The chip's full text, also used as its accessible text. */
export function chipText(
  outcome: ChipOutcome,
  label?: string | null,
  more = 0,
): string {
  const head =
    outcome === "pass" || outcome === "checking" || !label
      ? PREFIX[outcome]
      : `${PREFIX[outcome]}: ${label}`;
  return more > 0 ? `${head} (+${more} more)` : head;
}

export function CheckChip({
  outcome,
  label,
  more = 0,
  className = "",
}: CheckChipProps) {
  const visible =
    outcome === "pass" || outcome === "checking" || !label
      ? PREFIX[outcome]
      : label;
  const full = chipText(outcome, label, more);
  return (
    <span
      data-outcome={outcome}
      className={`inline-flex max-w-full items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${STYLE[outcome]} ${className}`}
    >
      <Icon outcome={outcome} />
      {full === visible ? (
        <span className="truncate">{visible}</span>
      ) : (
        <>
          <span className="sr-only">{full}</span>
          <span aria-hidden="true" className="truncate">
            {visible}
          </span>
        </>
      )}
      {more > 0 && <span aria-hidden="true">+{more}</span>}
    </span>
  );
}

export default CheckChip;
