/**
 * One lane row (R2.5, R2.6, R2.9, R4.5, R7.4, R8.6, R13.5, R19.3). A
 * `role="row" tabIndex={-1}` named with the lane summary. The body draws loads in Timeline
 * or Sequence layout, the paired driver's on-duty window and 11-hour driving
 * mark on today only, the "To reassign" shelf, pending commands as ghost
 * cards with "Checking…", and Place mode's "New load" slot. A collapsed lane
 * shows its header only and still takes drops (R4.5).
 */
import { Plus } from "lucide-react";
import { memo, useMemo } from "react";
import type { LaneView } from "../../../services/dispatchBoardApi";
import { useBoard } from "../BoardContext";
import { formatTime, HOUR_MS } from "../boardTime";
import { useRovingItem } from "../keyboard/roving";
import { GHOST_WIDTH, GhostLoad } from "../suggestions/GhostLoad";
import { isAccepted } from "../suggestions/suggestionModel";
import { hosHours } from "../trays/DriverTray";
import { LaneHeader, laneSummary } from "./LaneHeader";
import { LoadBlock } from "./LoadBlock";
import { CARD_WIDTH, type Ghost, laneLayout, PX_PER_HOUR } from "./layout";
import { ReassignShelf } from "./ReassignShelf";
import { CELL_SCROLL_MARGIN, GhostCard } from "./StopCard";

export interface LaneProps {
  lane: LaneView;
  rowIndex: number;
  height: number;
  window: { start: number; end: number };
  minWidth: number;
  collapsed: boolean;
}

function NewLoadSlot({ lane, left }: { lane: LaneView; left: number }) {
  const api = useBoard();
  const roving = useRovingItem(`new-load:${lane.truck_id}`);
  return (
    <div
      role="gridcell"
      tabIndex={-1}
      className="absolute inset-y-2"
      style={{ left }}
    >
      <button
        type="button"
        {...roving}
        aria-label={`Place in a new load on Truck ${lane.truck_id}`}
        onClick={(e) => {
          e.stopPropagation();
          api.placeOn({ target: "new-load", truckId: lane.truck_id });
        }}
        style={CELL_SCROLL_MARGIN}
        className="flex h-full min-w-24 items-center gap-1 rounded-md border border-dashed border-primary px-2 text-xs font-medium text-primary outline-none hover:bg-primary-soft focus-visible:ring-2 focus-visible:ring-primary"
      >
        <Plus className="h-3.5 w-3.5" aria-hidden="true" />
        New load
      </button>
    </div>
  );
}

function HosWindow({
  lane,
  window,
}: {
  lane: LaneView;
  window: { start: number; end: number };
}) {
  const api = useBoard();
  const duty = hosHours(lane.driver?.hos, "remaining_on_duty_window");
  const drive = hosHours(lane.driver?.hos, "remaining_drive_time");
  if (duty === null && drive === null) return null;
  const now = Date.now();
  const x = (ms: number) =>
    Math.max(0, ((ms - window.start) / HOUR_MS) * PX_PER_HOUR);
  const start = x(now);
  const dutyEnd = duty !== null ? now + duty * HOUR_MS : null;
  const driveEnd = drive !== null ? now + drive * HOUR_MS : null;
  const text = [
    dutyEnd ? `On-duty window ends ${formatTime(dutyEnd, api.timezone)}` : "",
    driveEnd ? `Driving limit at ${formatTime(driveEnd, api.timezone)}` : "",
  ]
    .filter(Boolean)
    .join(". ");
  return (
    <div
      data-testid="hos-window"
      className="pointer-events-none absolute inset-y-0"
    >
      <span className="sr-only">{text}.</span>
      {dutyEnd && (
        <div
          aria-hidden="true"
          className="absolute inset-y-0 bg-info-light/50"
          style={{ left: start, width: Math.max(0, x(dutyEnd) - start) }}
        />
      )}
      {driveEnd && (
        <div
          aria-hidden="true"
          title="11-hour driving limit"
          className="absolute inset-y-0 border-l-2 border-dashed border-warning"
          style={{ left: x(driveEnd) }}
        />
      )}
    </div>
  );
}

export const Lane = memo(function Lane({
  lane,
  rowIndex,
  height,
  window,
  minWidth,
  collapsed,
}: LaneProps) {
  const api = useBoard();
  const density = api.view.density;
  const { hidden, ghosts, pending } = useMemo(() => {
    const hidden = new Set<string>();
    const ghosts: Ghost[] = [];
    const pending: string[] = [];
    for (const e of api.optimistic) {
      if (e.item?.kind !== "order" && e.item?.kind !== "stop") continue;
      if (e.item.kind === "stop") for (const id of e.item.ids) hidden.add(id);
      if (e.toTruckId !== lane.truck_id) continue;
      const loadId = e.target?.load_id;
      e.item.ids.forEach((id, i) => {
        if (loadId && loadId !== "new") {
          ghosts.push({
            orderId: id,
            loadId,
            index: e.target?.index != null ? e.target.index + i : null,
          });
        } else {
          pending.push(id);
        }
      });
    }
    return { hidden, ghosts, pending };
  }, [api.optimistic, lane.truck_id]);

  const layout = useMemo(
    () =>
      laneLayout(lane, {
        zoom: api.view.zoom,
        density,
        window,
        hidden,
        ghosts,
      }),
    [lane, api.view.zoom, density, window, hidden, ghosts],
  );
  const placingStops =
    api.placeItem?.kind === "order" || api.placeItem?.kind === "stop";
  const cardW = CARD_WIDTH[density];
  let tail = Math.max(8, ...layout.loads.map((l) => l.left + l.width + 16));
  const pendingLeft = tail;
  if (pending.length) tail += pending.length * (cardW + 6) + 8;
  const newLoadLeft = tail;
  if (placingStops) tail += 120;
  const shelfLeft = tail;
  const showShelf = lane.shelf.length > 0;
  if (showShelf) tail += lane.shelf.length * (cardW + 4) + 96;
  // Open agent suggestions as ghost loads after the lane's own work (R16.1).
  const ghostLoads = api.snapshot.suggestions.filter(
    (s) => s.truck_id === lane.truck_id && !isAccepted(s, api.lanesById),
  );
  const ghostLeft = tail + 8;
  if (ghostLoads.length)
    tail = ghostLeft + ghostLoads.length * (GHOST_WIDTH + 8);
  const width = Math.max(minWidth, layout.width, tail);
  const showHos =
    api.isToday && layout.mode === "timeline" && Boolean(lane.driver?.hos);

  return (
    <div
      role="row"
      tabIndex={-1}
      aria-rowindex={rowIndex}
      aria-label={laneSummary(lane, api.snapshot.acknowledged)}
      data-lane-row={lane.truck_id}
      data-layout={layout.mode}
      className="flex border-b border-gray-200 bg-white"
      style={{ height }}
    >
      <LaneHeader lane={lane} etasUnavailable={layout.etasUnavailable} />
      {collapsed ? (
        <div
          role="gridcell"
          tabIndex={-1}
          className="flex items-center px-3 text-xs text-gray-600"
          style={{ width: minWidth }}
        >
          Collapsed · {lane.loads.reduce((n, l) => n + l.stops.length, 0)} stops
        </div>
      ) : (
        <div className="relative py-2" style={{ width }}>
          {showHos && <HosWindow lane={lane} window={window} />}
          <div className="relative h-full">
            {lane.loads.map((load, i) => {
              const box = layout.loads[i];
              if (!box) return null;
              return (
                <LoadBlock
                  key={load.load_id}
                  lane={lane}
                  load={load}
                  number={i + 1}
                  box={box}
                  placingStops={placingStops}
                />
              );
            })}
            {pending.map((id, i) => (
              <GhostCard
                key={`pending-${id}`}
                orderId={id}
                left={pendingLeft + i * (cardW + 6)}
              />
            ))}
            {placingStops && <NewLoadSlot lane={lane} left={newLoadLeft} />}
            {showShelf && <ReassignShelf lane={lane} left={shelfLeft} />}
            {ghostLoads.map((s, i) => (
              <GhostLoad
                key={s.suggestion_id}
                suggestion={s}
                left={ghostLeft + i * (GHOST_WIDTH + 8)}
              />
            ))}
            {lane.loads.length === 0 &&
              !placingStops &&
              pending.length === 0 &&
              ghostLoads.length === 0 && (
                <div className="absolute inset-y-0 left-2 flex items-center text-xs text-gray-500">
                  No loads yet. Drop an order on the truck header.
                </div>
              )}
          </div>
        </div>
      )}
    </div>
  );
});

export default Lane;
