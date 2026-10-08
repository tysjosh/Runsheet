/**
 * Reading agent suggestions (K9, R16.1–R16.3): their loads, why text and
 * whether they are already on the board.
 */
import type {
  BoardSuggestion,
  Check,
  LaneView,
} from "../../../services/dispatchBoardApi";
import type { ReplanDiff } from "../../../services/fuelApi";

export interface SuggestionLoad {
  loadKey: string;
  truckId: string;
  orderIds: string[];
  terminalId: string | null;
}

export function suggestionLoads(s: BoardSuggestion): SuggestionLoad[] {
  return s.loads.map((l, i) => ({
    loadKey:
      typeof l.load_key === "string" ? l.load_key : `${s.suggestion_id}-${i}`,
    truckId: typeof l.truck_id === "string" ? l.truck_id : s.truck_id,
    orderIds: Array.isArray(l.order_ids)
      ? l.order_ids.filter((o): o is string => typeof o === "string")
      : [],
    terminalId: typeof l.terminal_id === "string" ? l.terminal_id : null,
  }));
}

/** True once a load accepted from this suggestion is on any lane. */
export function isAccepted(
  s: BoardSuggestion,
  lanes: Record<string, LaneView>,
): boolean {
  return Object.values(lanes).some((lane) =>
    lane.loads.some((l) => l.suggestion_id === s.suggestion_id),
  );
}

/** The templated why sentence (K9); falls back to the agent's name. */
export function whyText(s: BoardSuggestion): string {
  const why = s.why;
  if (typeof why === "string" && why) return why;
  if (Array.isArray(why)) return why.join(". ");
  if (
    why &&
    typeof why === "object" &&
    typeof why.text === "string" &&
    why.text
  ) {
    return why.text;
  }
  return `Suggested by ${s.agent_name}.`;
}

export function diffOf(s: BoardSuggestion): ReplanDiff | null {
  const d = s.diff as Partial<ReplanDiff> | null;
  if (!d || typeof d !== "object") return null;
  return {
    diff_id: d.diff_id ?? s.suggestion_id,
    original_route_id: d.original_route_id ?? "",
    patched_route_id: d.patched_route_id ?? "",
    added_stops: d.added_stops ?? [],
    removed_stops: d.removed_stops ?? [],
    reordered_stops: d.reordered_stops ?? [],
    reassigned_stops: d.reassigned_stops ?? [],
    quantity_changes: d.quantity_changes ?? [],
    eta_shifts: d.eta_shifts ?? [],
    generated_at: d.generated_at ?? s.created_at ?? "",
  };
}

/**
 * After a refused accept: the loads whose truck had no blocking check, which
 * the dispatcher may accept on their own after confirming (R16.3).
 */
export function passingLoads(
  s: BoardSuggestion,
  checks: Check[],
): SuggestionLoad[] {
  const blockedTrucks = new Set(
    checks.filter((c) => c.outcome === "block").map((c) => c.scope.truck_id),
  );
  return suggestionLoads(s).filter((l) => !blockedTrucks.has(l.truckId));
}
