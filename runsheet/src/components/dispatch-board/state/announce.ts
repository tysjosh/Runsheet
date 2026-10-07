/**
 * Screen reader announcement text (design K14.6, R19).
 *
 * Pure string builders over server results. Names of other dispatchers come
 * from the server (`board_lanes_updated.actor.name`, K17.1); when none is
 * known the text says "Another dispatcher". Customer names are never used;
 * orders and trucks are named by id.
 */
import type {
  BoardCommand,
  Check,
  CheckOutcome,
  LaneView,
} from "../../../services/dispatchBoardApi";

export const ANOTHER_DISPATCHER = "Another dispatcher";
const ZERO_WIDTH_SPACE = "\u200B";
const RANK: Record<CheckOutcome, number> = {
  pass: 0,
  info: 1,
  warn: 2,
  block: 3,
};

/** The server-resolved name, or "Another dispatcher". */
export function actorDisplayName(name?: string | null): string {
  const trimmed = typeof name === "string" ? name.trim() : "";
  return trimmed || ANOTHER_DISPATCHER;
}

export function truckLabel(truckId: string): string {
  return `Truck ${truckId}`;
}

export function orderLabel(orderId: string): string {
  return `Order ${orderId}`;
}

const gallonsFormat = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
});

function sentence(text: string): string {
  const t = text.trim();
  if (!t) return "";
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

function lowerFirst(text: string): string {
  return text ? text.charAt(0).toLowerCase() + text.slice(1) : text;
}

interface StopPosition {
  loadNumber: number;
  stopNumber: number;
  loadId: string;
}

function findStop(lane: LaneView, orderId: string): StopPosition | null {
  for (let li = 0; li < lane.loads.length; li++) {
    const load = lane.loads[li];
    const si = load.stops.findIndex((s) => s.order_id === orderId);
    if (si >= 0) {
      return { loadNumber: li + 1, stopNumber: si + 1, loadId: load.load_id };
    }
  }
  return null;
}

/** "3,000 gallons diesel" / "Fill diesel", from the stop snapshot. */
function quantityText(lane: LaneView, orderId: string): string {
  for (const load of lane.loads) {
    const stop = load.stops.find((s) => s.order_id === orderId);
    if (!stop) continue;
    const product = stop.snapshot.product_code ?? "";
    if (stop.snapshot.fill_to_full) return `Fill ${product}`.trim();
    const g = stop.snapshot.gallons_requested;
    if (typeof g === "number") {
      return `${gallonsFormat.format(g)} gallons ${product}`.trim();
    }
    return product;
  }
  return "";
}

/** "Compartment 2 is 92% full" for the fullest compartment holding the order. */
function fillSentence(lane: LaneView, orderId: string, loadId: string): string {
  const load = lane.loads.find((l) => l.load_id === loadId);
  if (!load) return "";
  const mine = new Set(
    load.allocations
      .filter((a) => a.order_id === orderId)
      .map((a) => a.compartment_id),
  );
  let best: { id: string; pct: number } | null = null;
  for (const id of mine) {
    const rows = load.allocations.filter((a) => a.compartment_id === id);
    const capacity = rows[0]?.capacity_liters ?? 0;
    if (capacity <= 0) continue;
    const pct = Math.round(
      (rows.reduce((s, a) => s + a.liters, 0) / capacity) * 100,
    );
    if (!best || pct > best.pct) best = { id, pct };
  }
  if (!best) return "";
  const comp = lane.compartments.find((c) => c.compartment_id === best.id);
  const number = comp ? comp.position_index + 1 : best.id;
  return `Compartment ${number} is ${best.pct}% full.`;
}

/** The worst warn/block check closest to the scope (order, then load, then lane). */
export function worstCheck(
  checks: Check[],
  scope: { orderId?: string; loadId?: string } = {},
): Check | null {
  const tiers = [
    checks.filter((c) => scope.orderId && c.scope.order_id === scope.orderId),
    checks.filter((c) => scope.loadId && c.scope.load_id === scope.loadId),
    checks,
  ];
  for (const tier of tiers) {
    let worst: Check | null = null;
    for (const c of tier) {
      if (c.outcome !== "warn" && c.outcome !== "block") continue;
      if (!worst || RANK[c.outcome] > RANK[worst.outcome]) worst = c;
    }
    if (worst) return worst;
  }
  return null;
}

export function checkSentence(check: Check | null): string {
  if (!check) return "";
  const label = check.outcome === "block" ? "Blocked" : "Warning";
  return `${label}: ${sentence(lowerFirst(check.message))}`;
}

function placementText(
  verb: string,
  orderIds: string[],
  lane: LaneView | undefined,
  truckId: string,
): string {
  if (orderIds.length !== 1 || !lane) {
    const what =
      orderIds.length === 1
        ? orderLabel(orderIds[0])
        : `${orderIds.length} orders`;
    const worst = lane ? checkSentence(worstCheck(lane.checks)) : "";
    return [sentence(`${what} ${verb} ${truckLabel(truckId)}`), worst]
      .filter(Boolean)
      .join(" ");
  }
  const orderId = orderIds[0];
  const pos = findStop(lane, orderId);
  const qty = quantityText(lane, orderId);
  const head = [orderLabel(orderId), qty].filter(Boolean).join(", ");
  const where = pos
    ? `${truckLabel(truckId)}, load ${pos.loadNumber}, stop ${pos.stopNumber}`
    : truckLabel(truckId);
  const parts = [sentence(`${head}, ${verb} ${where}`)];
  if (pos) parts.push(fillSentence(lane, orderId, pos.loadId));
  parts.push(
    checkSentence(worstCheck(lane.checks, { orderId, loadId: pos?.loadId })),
  );
  return parts.filter(Boolean).join(" ");
}

/** The announcement for a committed command (R19.1). */
export function announceCommitted(
  command: BoardCommand,
  lanes: LaneView[],
): string {
  const lane = (t: string) => lanes.find((l) => l.truck_id === t);
  switch (command.type) {
    case "assign_orders":
      return placementText(
        "assigned to",
        command.order_ids,
        lane(command.truck_id),
        command.truck_id,
      );
    case "move_stops": {
      // A cross-lane move answers with the source lane too (R19.1 "from where").
      const from = lanes.find(
        (l) =>
          l.truck_id !== command.truck_id &&
          !command.order_ids.some((id) => findStop(l, id)),
      );
      return placementText(
        from ? `moved from ${truckLabel(from.truck_id)} to` : "moved to",
        command.order_ids,
        lane(command.truck_id),
        command.truck_id,
      );
    }
    case "unassign_orders": {
      const what =
        command.order_ids.length === 1
          ? orderLabel(command.order_ids[0])
          : `${command.order_ids.length} orders`;
      // Dispatched orders wait on the lane's shelf instead (R13.5).
      const shelved = lanes.find((l) =>
        command.order_ids.some((id) => l.shelf.includes(id)),
      );
      return shelved
        ? sentence(
            `${what} moved to the To reassign shelf on ${truckLabel(shelved.truck_id)}`,
          )
        : sentence(`${what} returned to the order tray`);
    }
    case "pair_driver": {
      if (command.driver_id === null)
        return sentence(`${truckLabel(command.truck_id)} has no driver`);
      const name =
        lane(command.truck_id)?.driver?.name || `Driver ${command.driver_id}`;
      const l = lane(command.truck_id);
      // The pairing moved off another truck (R6.2): name both.
      const from = lanes.find(
        (x) => x.truck_id !== command.truck_id && x.driver_id === null,
      );
      const head = from
        ? `${name} paired with ${truckLabel(command.truck_id)}, moved from ${truckLabel(from.truck_id)}`
        : `${name} paired with ${truckLabel(command.truck_id)}`;
      return [sentence(head), l ? checkSentence(worstCheck(l.checks)) : ""]
        .filter(Boolean)
        .join(" ");
    }
    case "move_load":
      return sentence(
        `Load moved to ${truckLabel(command.truck_id)}, position ${command.index + 1}`,
      );
    case "add_lane":
      return sentence(`${truckLabel(command.truck_id)} added to the board`);
    case "remove_lane":
      return sentence(`${truckLabel(command.truck_id)} removed from the board`);
    case "set_terminal":
      return command.terminal_id ? "Terminal set." : "Terminal cleared.";
    case "set_allocation":
      return command.shares
        ? "Compartment split updated."
        : "Compartment split reset.";
    case "set_load_shift":
      return "Load shift updated.";
    case "acknowledge_warning":
      return sentence(
        `Warning acknowledged on ${truckLabel(command.truck_id)}`,
      );
    case "accept_suggestion":
      return "Suggestion accepted.";
    case "discard_lane_changes":
      return sentence(`Changes discarded on ${truckLabel(command.truck_id)}`);
    case "revert":
      return "Change undone.";
    case "reapply":
      return "Change redone.";
    default:
      return "Change saved.";
  }
}

/** Conflict refusal (R14.2), for the assertive region. */
export function announceConflict(
  truckId: string,
  actorName?: string | null,
): string {
  return `${truckLabel(truckId)} was changed by ${actorDisplayName(actorName)}. Your change was not applied.`;
}

/** Block refusal: the first blocking reason and how many others. */
export function announceBlocked(checks: Check[]): string {
  const blocks = checks.filter((c) => c.outcome === "block");
  if (blocks.length === 0) return "Not applied. This change is blocked.";
  const more = blocks.length - 1;
  const tail =
    more > 0 ? ` ${more} more ${more === 1 ? "reason" : "reasons"}.` : "";
  return `Not applied. ${sentence(blocks[0].message)}${tail}`;
}

const UNDO_REASONS: Record<string, string> = {
  changed_by_other: "Another dispatcher changed this truck since.",
  published_since: "The truck was published since.",
  not_owner: "Only the dispatcher who made a change can undo it.",
};

/** Undo/redo refusal (R11.2). */
export function announceUndoRefused(
  reason?: string | null,
  redo = false,
): string {
  const verb = redo ? "redo" : "undo";
  return `Can't ${verb}. ${UNDO_REASONS[reason ?? ""] ?? "The board changed since."}`;
}

/** Publish result per lane (R12.8): failures go to the assertive region. */
export function announcePublishResult(
  truckId: string,
  state: string,
  writesMade?: boolean | "unknown" | null,
): { text: string; assertive: boolean } | null {
  if (state === "published") {
    return { text: `${truckLabel(truckId)} published.`, assertive: false };
  }
  if (state === "failed" || state === "recovering") {
    const tail =
      writesMade === false
        ? " Nothing was changed. Retry from the lane."
        : " Retry from the lane.";
    return {
      text: `Publish failed on ${truckLabel(truckId)}.${tail}`,
      assertive: true,
    };
  }
  return null;
}

export function announceNotSaved(): string {
  return "Not saved. Check your connection and retry.";
}

/**
 * Screen readers skip a repeated identical message; append a zero-width space
 * so consecutive identical announcements are read again (K14.6).
 */
export function distinctAnnouncement(previous: string, next: string): string {
  return previous === next ? `${next}${ZERO_WIDTH_SPACE}` : next;
}
