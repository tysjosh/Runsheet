/**
 * The single portal status map (R14.2, design §11.4).
 *
 * Every order, invoice and payment `status_code` the portal API can send maps
 * to one display status (hue + icon); the label is always the server's
 * `status_label`. Unknown codes fall back to the dashed `draft` style.
 *
 * `open` and `partial` are portal-local display statuses (D21). They are not
 * in `design/tokens.json` yet because Phase 3P leaves the shared tokens alone;
 * their colours are taken from the generated blue ramp with the same roles as
 * every other status (bg 100, fg 800, dot 600, border 300). Follow-up: move
 * them into `tokens.json` (Billing in Phase 3 needs them too).
 */
import { COLOR, STATUS, type StatusKey } from "../../styles/tokens";

export type PortalStatusKind = "order" | "invoice" | "payment";

/** Shared statuses plus the two portal-local invoice statuses. */
export type PortalStatusKey = StatusKey | "open" | "partial";

export interface PortalStatusToken {
  icon: string;
  label: string;
  strike: boolean;
  bg: string;
  fg: string;
  dot: string;
  border: string;
}

/** D21: Open and Partially paid share the blue family, told apart by icon. */
export const PORTAL_EXTRA_STATUS: Record<
  "open" | "partial",
  PortalStatusToken
> = {
  open: {
    icon: "FileText",
    label: "Open",
    strike: false,
    bg: COLOR.blue["100"],
    fg: COLOR.blue["800"],
    dot: COLOR.blue["600"],
    border: COLOR.blue["300"],
  },
  partial: {
    icon: "CircleDollarSign",
    label: "Partially paid",
    strike: false,
    bg: COLOR.blue["100"],
    fg: COLOR.blue["800"],
    dot: COLOR.blue["600"],
    border: COLOR.blue["300"],
  },
};

export function portalStatusToken(key: PortalStatusKey): PortalStatusToken {
  if (key === "open" || key === "partial") return PORTAL_EXTRA_STATUS[key];
  return STATUS[key];
}

export interface PortalStatusStyle {
  key: PortalStatusKey;
  /** Icon override (payment in flight shows a clock on the Open style). */
  icon?: string;
}

const ORDER: Record<string, PortalStatusStyle> = {
  awaiting_confirmation: { key: "draft" },
  on_hold: { key: "draft" },
  confirmed: { key: "planned" },
  out_for_delivery: { key: "in_transit" },
  delivered: { key: "delivered" },
  not_delivered: { key: "exception" },
  cancelled: { key: "cancelled" },
};

const INVOICE: Record<string, PortalStatusStyle> = {
  open: { key: "open" },
  partial: { key: "partial" },
  overdue: { key: "overdue" },
  paid: { key: "paid" },
  void: { key: "cancelled" },
};

const PAYMENT: Record<string, PortalStatusStyle> = {
  creating: { key: "open", icon: "Clock" },
  created: { key: "open", icon: "Clock" },
  pending: { key: "open", icon: "Clock" },
  succeeded: { key: "paid" },
  failed: { key: "exception" },
  canceled: { key: "exception" },
};

const MAPS: Record<PortalStatusKind, Record<string, PortalStatusStyle>> = {
  order: ORDER,
  invoice: INVOICE,
  payment: PAYMENT,
};

/** Display style for a server status code; unknown codes → dashed draft. */
export function portalStatusStyle(
  kind: PortalStatusKind,
  code: string | null | undefined,
): PortalStatusStyle {
  return MAPS[kind][code ?? ""] ?? { key: "draft" };
}

/** Payment attempt codes during which Pay is replaced by the status (R14.13). */
export const PAYMENT_IN_FLIGHT: ReadonlySet<string> = new Set([
  "creating",
  "created",
  "pending",
]);

/** Order codes that are finished; everything else is "active" (R14.7). */
export const PAST_ORDER_CODES: ReadonlySet<string> = new Set([
  "delivered",
  "not_delivered",
  "cancelled",
]);
