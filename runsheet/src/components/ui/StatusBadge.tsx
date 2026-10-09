/**
 * StatusBadge: hue + icon + label for every status (R5.4, design.md §3).
 *
 * Colour is never the only signal: each status has its own icon and the label
 * is always printed. `cancelled` is struck through; `draft` has a dashed
 * outline. Colours come from the generated tokens (100 bg, 800 text, 300
 * border), all ≥ 4.5:1.
 */
import {
  BadgeCheck,
  CalendarClock,
  CalendarX,
  Check,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  CircleDollarSign,
  Clock,
  FileText,
  type LucideIcon,
  OctagonAlert,
  Send,
  TriangleAlert,
  Truck,
  X,
} from "lucide-react";
import { STATUS, type StatusKey } from "../../styles/tokens";

export const STATUS_ICONS: Record<string, LucideIcon> = {
  CircleDashed,
  CalendarClock,
  Send,
  Truck,
  Check,
  Clock,
  TriangleAlert,
  X,
  CircleCheck,
  CircleAlert,
  OctagonAlert,
  BadgeCheck,
  CalendarX,
  FileText,
  CircleDollarSign,
};

/**
 * Maps the backend's many status vocabularies (jobs, orders, plans, loads,
 * invoices) onto the 13 display statuses. Unknown values return null so a
 * caller can fall back to plain text.
 */
const ALIASES: Record<string, StatusKey> = {
  draft: "draft",
  pending: "draft",
  new: "draft",
  placed: "draft",
  planned: "planned",
  scheduled: "planned",
  confirmed: "planned",
  proposed: "planned",
  assigned: "dispatched",
  dispatched: "dispatched",
  published: "dispatched",
  in_transit: "in_transit",
  in_progress: "in_transit",
  en_route: "in_transit",
  active: "in_transit",
  delivered: "delivered",
  completed: "delivered",
  complete: "delivered",
  done: "delivered",
  delayed: "delayed",
  late: "delayed",
  on_hold: "delayed",
  exception: "exception",
  failed: "exception",
  error: "exception",
  rejected: "exception",
  cancelled: "cancelled",
  canceled: "cancelled",
  void: "cancelled",
  voided: "cancelled",
  ok: "ok",
  healthy: "ok",
  warning: "warning",
  degraded: "warning",
  critical: "critical",
  paid: "paid",
  overdue: "overdue",
};

export function statusKeyFor(raw: string | null | undefined): StatusKey | null {
  if (!raw) return null;
  const key = raw
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_");
  if (key in STATUS) return key as StatusKey;
  return ALIASES[key] ?? null;
}

export interface StatusBadgeProps {
  status: StatusKey;
  /** Overrides the token label (e.g. "On hold" shown in the Delayed style). */
  label?: string;
  size?: "sm" | "md";
  /** Rendered after the label, e.g. a filter count. */
  count?: number;
  /**
   * Icon name from `STATUS_ICONS` overriding the token's (e.g. a payment in
   * flight shows `Clock` on the Open style). Additive (task 3.11).
   */
  icon?: string;
  className?: string;
}

export function StatusBadge({
  status,
  label,
  size = "sm",
  count,
  icon,
  className = "",
}: StatusBadgeProps) {
  const token = STATUS[status];
  const iconName = icon ?? token.icon;
  const Icon = STATUS_ICONS[iconName] ?? CircleDashed;
  const text = label ?? token.label;
  const dims =
    size === "md" ? "h-6 px-2.5 text-xs gap-1.5" : "h-5 px-2 text-xs gap-1";
  return (
    <span
      data-status={status}
      data-icon={iconName}
      className={`inline-flex shrink-0 items-center whitespace-nowrap rounded-full border font-semibold leading-none ${dims} ${className}`}
      style={{
        backgroundColor: token.bg,
        color: token.fg,
        borderColor: token.border,
        borderStyle: status === "draft" ? "dashed" : "solid",
      }}
    >
      <Icon
        aria-hidden="true"
        className={size === "md" ? "h-3.5 w-3.5" : "h-3 w-3"}
        style={{ color: token.fg }}
      />
      <span className={token.strike ? "line-through" : undefined}>{text}</span>
      {count !== undefined && (
        <span className="tabular-nums opacity-90">{count}</span>
      )}
    </span>
  );
}
