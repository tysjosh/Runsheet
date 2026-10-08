/**
 * Drawer History tab (R22.3, K17): the lane's command log from
 * `GET /history?truck_id=`, newest first: who, when, what, the check outcome
 * and any override reasons. Reasons are rendered as text (React escapes them).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getBoardHistory,
  type HistoryItem,
} from "../../../services/dispatchBoardApi";
import { actorDisplayName } from "../state/announce";

export const HISTORY_PAGE_SIZE = 20;

const COMMAND_TEXT: Record<string, string> = {
  add_lane: "Added the truck",
  remove_lane: "Removed the truck",
  pair_driver: "Changed the driver",
  assign_orders: "Assigned orders",
  move_stops: "Moved stops",
  unassign_orders: "Unassigned orders",
  move_load: "Moved a load",
  set_terminal: "Set the terminal",
  set_allocation: "Changed the compartment split",
  set_load_shift: "Changed the load shift",
  acknowledge_warning: "Acknowledged a warning",
  accept_suggestion: "Accepted a suggestion",
  discard_lane_changes: "Discarded changes",
  revert: "Undid a change",
  reapply: "Redid a change",
};

const RESULT_TEXT: Record<string, string> = {
  committed: "Saved",
  refused: "Not applied",
  blocked: "Blocked",
  conflict: "Conflict",
};

export function commandText(type: string): string {
  return COMMAND_TEXT[type] ?? type.replace(/_/g, " ");
}

function whenText(iso: string | null, timeZone: string): string {
  if (!iso) return "";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "";
  try {
    return new Intl.DateTimeFormat("en-US", {
      timeZone,
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    }).format(new Date(ms));
  } catch {
    return "";
  }
}

function overrideReasons(item: HistoryItem): string[] {
  return item.overrides
    .map((o) => (typeof o.reason === "string" ? o.reason : null))
    .filter((r): r is string => Boolean(r));
}

/** The lane's check outcome after the command (`checks_summary[truck].outcome`). */
function worstOutcome(item: HistoryItem, truckId: string): string | null {
  const entry = item.checks_summary[truckId] as
    | { outcome?: unknown }
    | undefined;
  const worst = typeof entry?.outcome === "string" ? entry.outcome : null;
  if (worst === "block") return "blocked";
  if (worst === "warn") return "with warnings";
  return null;
}

export function HistoryTab({
  serviceDate,
  truckId,
  version,
  timeZone,
}: {
  serviceDate: string;
  truckId: string;
  /** Lane version; a new version reloads the first page. */
  version: number;
  timeZone: string;
}) {
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "failed">(
    "loading",
  );
  const request = useRef(0);

  const load = useCallback(
    async (after: string | null) => {
      const n = ++request.current;
      if (!after) setStatus("loading");
      try {
        const page = await getBoardHistory(serviceDate, {
          truck_id: truckId,
          size: HISTORY_PAGE_SIZE,
          ...(after ? { cursor: after } : {}),
        });
        if (n !== request.current) return;
        setItems((prev) => (after ? [...prev, ...page.items] : page.items));
        setCursor(page.next_cursor);
        setStatus("ready");
      } catch {
        if (n === request.current) setStatus("failed");
      }
    },
    [serviceDate, truckId],
  );

  useEffect(() => {
    void load(null);
  }, [load, version]);

  if (status === "loading" && items.length === 0) {
    return (
      <p role="status" className="text-sm text-gray-600">
        Loading history…
      </p>
    );
  }
  if (status === "failed" && items.length === 0) {
    return (
      <div className="text-sm text-gray-700">
        <p>History couldn't be loaded.</p>
        <button
          type="button"
          onClick={() => void load(null)}
          className="mt-1 min-h-7 rounded-md border border-gray-300 px-2 text-xs font-medium"
        >
          Try again
        </button>
      </div>
    );
  }
  if (items.length === 0) {
    return <p className="text-sm text-gray-600">No changes yet.</p>;
  }
  return (
    <div>
      <ol
        aria-label="Changes, newest first"
        className="divide-y divide-gray-100"
      >
        {items.map((item) => {
          const reasons = overrideReasons(item);
          const worst = worstOutcome(item, truckId);
          return (
            <li key={item.command_id} className="py-2 text-sm">
              <p className="text-gray-900">
                <span className="font-medium">
                  {actorDisplayName(item.actor_name)}
                </span>{" "}
                {commandText(item.type).toLowerCase()}
                {item.result !== "committed"
                  ? ` (${RESULT_TEXT[item.result] ?? item.result})`
                  : ""}
                {worst ? `, ${worst}` : ""}
              </p>
              <p className="text-xs text-gray-600">
                {whenText(item.created_at, timeZone)}
                {item.input_modality ? ` · by ${item.input_modality}` : ""}
              </p>
              {reasons.map((r, i) => (
                <p key={i} className="mt-0.5 text-xs text-gray-700">
                  Reason: {r}
                </p>
              ))}
            </li>
          );
        })}
      </ol>
      {cursor && (
        <button
          type="button"
          onClick={() => void load(cursor)}
          className="mt-2 min-h-7 rounded-md border border-gray-300 px-2 text-xs font-medium"
        >
          Show older changes
        </button>
      )}
    </div>
  );
}

export default HistoryTab;
