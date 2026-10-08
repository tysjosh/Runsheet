/**
 * The single portal status map (R14.2, design §11.4).
 *
 * Every order, invoice and payment `status_code` the portal API can send maps
 * to one display status (hue + icon); the label is always the server's
 * `status_label`. Unknown codes fall back to the dashed `draft` style.
 * `open` and `partial` (D21) are shared status tokens since task 3.11.
 */
import type { StatusKey } from "../../styles/tokens";
export type PortalStatusKind = "order" | "invoice" | "payment";
export type PortalStatusKey = StatusKey;
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
