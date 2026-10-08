/**
 * "Assign to truck…", "Move to…", "Pair with truck…" and "Move load to…"
 * (design K14.4, R18.1, R18.2). A searchable list of lanes, each with its
 * validation outcome from one `validateBoard` call. Lanes with a `block` are
 * `aria-disabled` and say why in their accessible name and visible text.
 * Choosing a lane sends the same command a drop on its header sends (best
 * fit); "Position…" lists the slots in that lane and validates the focused
 * one, like hovering a slot during a drag.
 */
import { ChevronLeft } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  type CandidateResult,
  type LaneView,
  validateBoard,
} from "../../../services/dispatchBoardApi";
import { Modal } from "../../ui";
import type { AssignMode } from "../BoardContext";
import { useBoard } from "../BoardContext";
import { CheckChip } from "../CheckChip";
import { type BoardItem, type BoardTarget, engineIndex } from "../intents";
import { POSITION_DEBOUNCE_MS } from "../useBoardController";

export interface AssignToMenuProps {
  mode: AssignMode;
  item: BoardItem;
  originKey: string | null;
  onClose: () => void;
  validate?: typeof validateBoard;
}

const TITLE: Record<AssignMode, string> = {
  assign: "Assign to truck",
  move: "Move to",
  pair: "Pair with truck",
  load: "Move load to",
};

/** First blocking (else warning) reason of a result, as text. */
export function resultReason(result: CandidateResult | undefined): string {
  if (!result) return "";
  const worst =
    result.worst_checks.find((c) => c.outcome === result.outcome) ??
    result.worst_checks[0];
  if (worst?.message) return worst.message;
  if (result.reason) return result.reason.replace(/_/g, " ");
  return "";
}

function itemText(item: BoardItem): string {
  if (item.kind === "driver") return `Driver ${item.ids[0]}`;
  if (item.kind === "load") return "the load";
  return item.ids.length === 1
    ? `Order ${item.ids[0]}`
    : `${item.ids.length} orders`;
}

interface Slot {
  key: string;
  label: string;
  target: BoardTarget;
  loadId: string | null;
  index: number | null;
}

function slotsFor(lane: LaneView): Slot[] {
  const out: Slot[] = [];
  lane.loads.forEach((load, li) => {
    const n = load.stops.length;
    for (let i = 0; i <= n; i++) {
      const where =
        n === 0
          ? "first stop"
          : i === 0
            ? `before stop 1 (Order ${load.stops[0].order_id})`
            : `after stop ${i} (Order ${load.stops[i - 1].order_id})`;
      out.push({
        key: `${load.load_id}:${i}`,
        label: `Load ${li + 1}, ${where}`,
        target: {
          target: "load",
          truckId: lane.truck_id,
          loadId: load.load_id,
          index: i,
        },
        loadId: load.load_id,
        index: i,
      });
    }
  });
  out.push({
    key: "new",
    label: "New load",
    target: { target: "new-load", truckId: lane.truck_id },
    loadId: null,
    index: null,
  });
  return out;
}

export function AssignToMenu({
  mode,
  item,
  originKey,
  onClose,
  validate = validateBoard,
}: AssignToMenuProps) {
  const api = useBoard();
  const lanes = useMemo(
    () =>
      api.state.laneOrder
        .map((t) => api.lanesById[t])
        .filter((l): l is LaneView => Boolean(l)),
    [api.state.laneOrder, api.lanesById],
  );
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Record<string, CandidateResult>>({});
  const [checking, setChecking] = useState(true);
  const [laneForSlots, setLaneForSlots] = useState<string | null>(null);
  const [slotResult, setSlotResult] = useState<{
    key: string;
    result: CandidateResult | null;
  } | null>(null);
  const slotTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const slotAbort = useRef<AbortController | null>(null);
  const serviceDate = api.serviceDate;
  const itemKey = `${item.kind}:${item.ids.join(",")}`;

  // One validate call for every lane when the list opens (R18.2).
  useEffect(() => {
    const candidates = lanes.map((l) => l.truck_id).slice(0, 60);
    if (candidates.length === 0) {
      setChecking(false);
      return;
    }
    const controller = new AbortController();
    setChecking(true);
    void validate(
      serviceDate,
      { item: { kind: item.kind, ids: item.ids }, candidates },
      controller.signal,
    )
      .then((res) => {
        if (!controller.signal.aborted) setResults(res.results);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!controller.signal.aborted) setChecking(false);
      });
    return () => controller.abort();
    // Re-validate only when the item or day changes, not on lane updates.
  }, [itemKey, serviceDate, validate]);

  useEffect(
    () => () => {
      if (slotTimer.current) clearTimeout(slotTimer.current);
      slotAbort.current?.abort();
    },
    [],
  );

  const q = query.trim().toLowerCase();
  const visible = lanes.filter(
    (l) =>
      !q ||
      l.truck_id.toLowerCase().includes(q) ||
      (l.driver?.name ?? "").toLowerCase().includes(q),
  );

  const choose = (target: BoardTarget) => {
    onClose();
    api.perform(item, target, "menu", originKey);
  };

  const laneTarget = (truckId: string): BoardTarget =>
    mode === "pair"
      ? { target: "driver-slot", truckId }
      : { target: "lane", truckId };

  const validateSlot = (lane: LaneView, slot: Slot) => {
    if (slotTimer.current) clearTimeout(slotTimer.current);
    slotAbort.current?.abort();
    if (slot.loadId === null || slot.index === null) {
      setSlotResult(null);
      return;
    }
    setSlotResult({ key: slot.key, result: null });
    const position = {
      load_id: slot.loadId,
      index: engineIndex(
        lane,
        slot.loadId,
        slot.index,
        item.kind === "stop" ? item.ids : [],
      ),
    };
    slotTimer.current = setTimeout(() => {
      const controller = new AbortController();
      slotAbort.current = controller;
      void validate(
        serviceDate,
        {
          item: { kind: item.kind, ids: item.ids },
          candidates: [lane.truck_id],
          position,
        },
        controller.signal,
      )
        .then((res) => {
          if (!controller.signal.aborted) {
            setSlotResult({
              key: slot.key,
              result: res.results[lane.truck_id] ?? null,
            });
          }
        })
        .catch(() => undefined);
    }, POSITION_DEBOUNCE_MS);
  };

  const slotLane = laneForSlots ? api.lanesById[laneForSlots] : undefined;
  const title = slotLane
    ? `Position on Truck ${slotLane.truck_id}`
    : `${TITLE[mode]}: ${itemText(item)}`;

  return (
    <Modal isOpen onClose={onClose} title={title} size="md">
      {slotLane ? (
        <div>
          <button
            type="button"
            className="mb-2 inline-flex min-h-9 items-center gap-1 text-sm font-medium text-primary hover:underline"
            onClick={() => {
              setLaneForSlots(null);
              setSlotResult(null);
            }}
          >
            <ChevronLeft className="h-4 w-4" aria-hidden="true" />
            All trucks
          </button>
          <ul
            aria-label="Positions"
            className="max-h-96 space-y-1 overflow-auto"
          >
            {slotsFor(slotLane).map((slot) => {
              const r = slotResult?.key === slot.key ? slotResult : null;
              const delta = r?.result?.preview.eta_delta_minutes;
              const blocked = r?.result?.outcome === "block";
              const reason = resultReason(r?.result ?? undefined);
              return (
                <li key={slot.key}>
                  <button
                    type="button"
                    aria-disabled={blocked || undefined}
                    aria-label={`${slot.label}${r?.result ? `, ${r.result.outcome === "pass" ? "OK" : `${r.result.outcome === "block" ? "blocked" : r.result.outcome}${reason ? `: ${reason}` : ""}`}` : ""}${typeof delta === "number" ? `, ${delta >= 0 ? "+" : ""}${Math.round(delta)} min` : ""}`}
                    onFocus={() => validateSlot(slotLane, slot)}
                    onMouseEnter={() => validateSlot(slotLane, slot)}
                    onClick={() => !blocked && choose(slot.target)}
                    className={`flex min-h-10 w-full items-center justify-between gap-2 rounded-md border px-3 py-1.5 text-left text-sm ${
                      blocked
                        ? "cursor-not-allowed border-gray-200 text-gray-400"
                        : "border-gray-200 hover:bg-gray-50"
                    }`}
                  >
                    <span>{slot.label}</span>
                    <span className="flex items-center gap-2">
                      {typeof delta === "number" && (
                        <span className="text-xs text-gray-600">
                          {delta >= 0 ? "+" : ""}
                          {Math.round(delta)} min
                        </span>
                      )}
                      {r && (
                        <CheckChip
                          outcome={r.result ? r.result.outcome : "checking"}
                          label={reason}
                        />
                      )}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ) : (
        <div>
          <label className="sr-only" htmlFor="assign-search">
            Search trucks
          </label>
          <input
            id="assign-search"
            type="search"
            placeholder="Search trucks or drivers"
            value={query}
            maxLength={100}
            onChange={(e) => setQuery(e.target.value)}
            className="mb-3 min-h-9 w-full rounded-md border border-gray-300 px-2 text-sm"
          />
          {checking && (
            <p role="status" className="mb-2 text-xs text-gray-600">
              Checking trucks…
            </p>
          )}
          {visible.length === 0 ? (
            <p className="text-sm text-gray-600">No trucks match.</p>
          ) : (
            <ul
              aria-label="Trucks"
              className="max-h-96 space-y-1 overflow-auto"
            >
              {visible.map((lane) => {
                const r = results[lane.truck_id];
                const blocked = r?.outcome === "block";
                const reason = resultReason(r);
                const outcomeText = r
                  ? r.outcome === "pass"
                    ? "OK"
                    : `${r.outcome === "block" ? "blocked" : r.outcome === "warn" ? "warning" : "info"}${reason ? `: ${reason}` : ""}`
                  : checking
                    ? "checking"
                    : "";
                const driver = lane.driver?.name ?? "no driver";
                return (
                  <li key={lane.truck_id} className="flex items-stretch gap-1">
                    <button
                      type="button"
                      aria-disabled={blocked || undefined}
                      aria-label={`Truck ${lane.truck_id}, ${driver}${outcomeText ? `, ${outcomeText}` : ""}`}
                      onClick={() =>
                        !blocked && choose(laneTarget(lane.truck_id))
                      }
                      className={`flex min-h-10 flex-1 items-center justify-between gap-2 rounded-md border px-3 py-1.5 text-left text-sm ${
                        blocked
                          ? "cursor-not-allowed border-gray-200 text-gray-400"
                          : "border-gray-200 hover:bg-gray-50"
                      }`}
                    >
                      <span>
                        <span className="font-medium">
                          Truck {lane.truck_id}
                        </span>
                        <span className="ml-2 text-gray-600">{driver}</span>
                      </span>
                      {r ? (
                        <CheckChip outcome={r.outcome} label={reason} />
                      ) : checking ? (
                        <CheckChip outcome="checking" />
                      ) : null}
                    </button>
                    {(mode === "assign" || mode === "move") && !blocked && (
                      <button
                        type="button"
                        aria-label={`Position on Truck ${lane.truck_id}`}
                        onClick={() => setLaneForSlots(lane.truck_id)}
                        className="min-h-10 rounded-md border border-gray-200 px-2 text-xs font-medium text-gray-700 hover:bg-gray-50"
                      >
                        Position…
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </Modal>
  );
}

export default AssignToMenu;
