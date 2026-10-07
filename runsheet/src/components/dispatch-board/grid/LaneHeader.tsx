/**
 * Lane header (R2.8, R5.1, R5.4, R6.1, R6.5, R7.5, R14.4, R19.3, R19.4).
 * Sticky on the left of each lane row. Two grid cells:
 *
 * - the truck button: drop an order here for best fit; in Place mode it
 *   places the selection; otherwise it opens the lane menu (Pair, Unpair,
 *   Collapse, Remove lane — disabled with its reason once ever published);
 * - the driver slot: drop a driver here; "Pair {name}" pairs the truck's
 *   permanent driver with one click (K5.2).
 *
 * Badges carry text (state, HOS today only, certification, cleaning, warning
 * count, "Ana is editing", "Checking…"); the gauge shows the selected load
 * and, during a drag, the server's post-drop fill.
 */
import { useRef } from "react";
import type { LaneState, LaneView } from "../../../services/dispatchBoardApi";
import { Badge } from "../../ui";
import { focusKey, useBoard } from "../BoardContext";
import { laneMatch, matchClass } from "../boardMatch";
import { CheckChip } from "../CheckChip";
import { CompartmentGauge, previewFromChecks } from "../CompartmentGauge";
import { resultReason } from "../dialogs/AssignToMenu";
import { useDropTarget } from "../dnd/adapter";
import { useRovingItem } from "../keyboard/roving";
import { laneMenu } from "../menus";
import { isLanePending } from "../state/boardReducer";
import { hosHours } from "../trays/DriverTray";
import { HEADER_WIDTH } from "./layout";

export const LANE_STATE_LABEL: Record<
  LaneState,
  {
    label: string;
    variant: "neutral" | "success" | "warning" | "info" | "error";
  }
> = {
  draft: { label: "Draft", variant: "neutral" },
  published: { label: "Published", variant: "success" },
  modified: { label: "Modified", variant: "warning" },
  publishing: { label: "Publishing", variant: "info" },
  failed: { label: "Publish failed", variant: "error" },
  recovering: { label: "Recovering", variant: "error" },
};

/** Open warnings / blocks on the lane (not acknowledged). */
export function openIssues(
  lane: LaneView,
  acknowledged: Record<string, unknown>,
) {
  return lane.checks.filter(
    (c) =>
      (c.outcome === "warn" || c.outcome === "block") &&
      !(c.warning_id && acknowledged[c.warning_id]),
  );
}

/** The lane's accessible summary (R19.3). */
export function laneSummary(
  lane: LaneView,
  acknowledged: Record<string, unknown>,
): string {
  const stops = lane.loads.reduce((n, l) => n + l.stops.length, 0);
  let fullest = 0;
  for (const load of lane.loads) {
    for (const c of lane.compartments) {
      const liters = load.allocations
        .filter((a) => a.compartment_id === c.compartment_id)
        .reduce((s, a) => s + a.liters, 0);
      if (c.capacity_l > 0)
        fullest = Math.max(fullest, Math.round((liters / c.capacity_l) * 100));
    }
  }
  const warnings = openIssues(lane, acknowledged).length;
  const parts = [
    `Truck ${lane.truck_id}`,
    lane.driver?.name
      ? `driver ${lane.driver.name}`
      : lane.driver_id
        ? `driver ${lane.driver_id}`
        : "no driver",
    `${lane.loads.length} ${lane.loads.length === 1 ? "load" : "loads"}`,
    `${stops} ${stops === 1 ? "stop" : "stops"}`,
  ];
  if (lane.loads.length > 0) parts.push(`fullest compartment ${fullest}%`);
  parts.push(`${warnings} ${warnings === 1 ? "warning" : "warnings"}`);
  parts.push(LANE_STATE_LABEL[lane.state].label);
  return parts.join(", ");
}

export interface LaneHeaderProps {
  lane: LaneView;
  etasUnavailable: boolean;
}

export function LaneHeader({ lane, etasUnavailable }: LaneHeaderProps) {
  const api = useBoard();
  const truckRef = useRef<HTMLButtonElement>(null);
  const driverRef = useRef<HTMLButtonElement>(null);
  const truckRoving = useRovingItem(focusKey.lane(lane.truck_id));
  const driverRoving = useRovingItem(focusKey.driverSlot(lane.truck_id));
  const overLane = useDropTarget(truckRef, {
    target: { target: "lane", truckId: lane.truck_id },
    enabled: true,
    readOnly: api.readOnly,
  });
  const overDriver = useDropTarget(driverRef, {
    target: { target: "driver-slot", truckId: lane.truck_id },
    enabled: true,
    readOnly: api.readOnly,
  });
  const state = LANE_STATE_LABEL[lane.state];
  const issues = openIssues(lane, api.snapshot.acknowledged);
  const worst = issues.find((c) => c.outcome === "block") ?? issues[0] ?? null;
  const place = api.placeItem;
  const candidate = api.dragItem ? api.candidate(lane.truck_id) : undefined;
  const pending = isLanePending(api.state, lane.truck_id);
  const editor = api.laneEditor(lane.truck_id);
  const compact = api.view.density === "compact";
  const lookup = laneMatch(lane, api.view.search);

  // Gauge: the load holding the selection, else the first load.
  const selectedLoad =
    lane.loads.find((l) =>
      l.stops.some((s) => api.isSelected("stop", s.order_id)),
    ) ?? lane.loads[0];
  const preview = candidate?.preview.load_id
    ? previewFromChecks(
        candidate.preview.fill_by_compartment,
        candidate.worst_checks,
      )
    : null;
  const gaugeLoad =
    preview && candidate?.preview.load_id
      ? (lane.loads.find((l) => l.load_id === candidate.preview.load_id) ??
        selectedLoad)
      : selectedLoad;

  const placeLabel =
    place && (place.kind === "order" || place.kind === "stop")
      ? `Place ${place.ids.length === 1 ? `Order ${place.ids[0]}` : `${place.ids.length} orders`} on Truck ${lane.truck_id}`
      : place?.kind === "load"
        ? `Move the load to Truck ${lane.truck_id}`
        : place?.kind === "driver"
          ? `Place Driver ${place.ids[0]} on Truck ${lane.truck_id}`
          : null;

  const onTruck = () => {
    if (place && placeLabel) {
      api.placeOn({ target: "lane", truckId: lane.truck_id });
      return;
    }
    api.showMenu({
      label: `Truck ${lane.truck_id} actions`,
      items: laneMenu(api, lane),
      anchor: truckRef.current,
    });
  };

  const driverName = lane.driver?.name ?? lane.driver_id;
  const suggested = !lane.driver_id ? lane.suggested_driver : null;
  const placingDriver = place?.kind === "driver";
  const driverLabel = placingDriver
    ? `Pair Driver ${place.ids[0]} with Truck ${lane.truck_id}`
    : suggested
      ? `Pair ${suggested.name}`
      : driverName
        ? `Driver ${driverName}`
        : "No driver";
  const onDriver = () => {
    if (placingDriver) {
      api.placeOn({ target: "driver-slot", truckId: lane.truck_id });
    } else if (suggested) {
      api.perform(
        { kind: "driver", ids: [suggested.driver_id] },
        { target: "driver-slot", truckId: lane.truck_id },
        "menu",
        focusKey.driverSlot(lane.truck_id),
      );
    } else {
      api.announce(
        "Select a driver in the driver tray, then choose this driver slot.",
      );
    }
  };

  const hos = api.isToday
    ? hosHours(lane.driver?.hos, "remaining_drive_time")
    : null;
  const cert = lane.checks.find(
    (c) =>
      c.check === "asset_certification" &&
      (c.outcome === "block" || c.outcome === "warn"),
  );
  const cleaning = lane.compartments.some((c) => c.state === "needs_cleaning");

  return (
    <div
      className={`sticky left-0 z-10 flex shrink-0 flex-col gap-1 border-r border-gray-200 bg-white px-2 py-1 ${matchClass(lookup)}`}
      style={{ width: HEADER_WIDTH }}
    >
      <div className="flex items-center gap-1">
        <div role="rowheader" tabIndex={-1} className="min-w-0">
          <button
            ref={truckRef}
            type="button"
            {...truckRoving}
            aria-haspopup={placeLabel ? undefined : "menu"}
            aria-label={placeLabel ?? `Truck ${lane.truck_id} actions`}
            onClick={onTruck}
            className={`min-h-7 rounded-md px-1 text-left text-sm font-semibold text-gray-900 outline-none hover:bg-gray-50 focus-visible:ring-2 focus-visible:ring-primary ${
              overLane || placeLabel ? "ring-2 ring-primary" : ""
            }`}
          >
            <span>Truck {lane.truck_id}</span>
          </button>
        </div>
        <Badge variant={state.variant} size="sm">
          {state.label}
        </Badge>
        {candidate ? (
          <CheckChip
            outcome={candidate.outcome}
            label={resultReason(candidate)}
            more={Math.max(0, candidate.worst_checks.length - 1)}
          />
        ) : pending ? (
          <CheckChip outcome="checking" />
        ) : worst ? (
          <CheckChip
            outcome={worst.outcome}
            label={`${issues.length} ${issues.length === 1 ? "issue" : "issues"}`}
          />
        ) : null}
      </div>
      <div
        role="gridcell"
        tabIndex={-1}
        className="flex min-w-0 items-center gap-1 overflow-hidden whitespace-nowrap"
      >
        <button
          ref={driverRef}
          type="button"
          {...driverRoving}
          aria-label={driverLabel}
          onClick={onDriver}
          className={`min-h-6 max-w-40 truncate rounded-md border px-1.5 text-left text-xs outline-none focus-visible:ring-2 focus-visible:ring-primary ${
            suggested && !placingDriver
              ? "border-primary text-primary"
              : "border-gray-300 text-gray-800"
          } ${overDriver || placingDriver ? "ring-2 ring-primary" : ""}`}
        >
          {driverLabel}
        </button>
        {hos !== null && (
          <span className="text-[11px] text-gray-600">
            HOS {hos.toFixed(1)} h
          </span>
        )}
        {editor && (
          <span className="text-[11px] font-medium text-info-dark">
            {editor} is editing
          </span>
        )}
        {cert && (
          <span className="text-[11px] text-error-dark">
            Certification {cert.outcome === "block" ? "blocked" : "warning"}
          </span>
        )}
        {cleaning && (
          <span className="text-[11px] text-warning-dark">Needs cleaning</span>
        )}
        {etasUnavailable && (
          <span className="text-[11px] text-gray-600">ETAs unavailable</span>
        )}
      </div>
      {(gaugeLoad || preview) && (
        <CompartmentGauge
          compartments={lane.compartments}
          allocations={gaugeLoad?.allocations ?? []}
          preview={preview}
          compact={compact}
        />
      )}
    </div>
  );
}

export default LaneHeader;
