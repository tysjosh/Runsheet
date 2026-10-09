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

import { TriangleAlert } from "lucide-react";
import { useRef } from "react";
import {
  date as formatDate,
  number as formatNumber,
} from "../../../lib/format";
import { identityFor, initials } from "../../../lib/identity";
import type { LaneState, LaneView } from "../../../services/dispatchBoardApi";
import type { StatusKey } from "../../../styles/tokens";
import { IdentityAvatar, StatusBadge } from "../../ui";
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

/** Lane state as a StatusBadge: hue + icon + label (R8.4, colour never alone). */
export const LANE_STATE_LABEL: Record<
  LaneState,
  { label: string; status: StatusKey }
> = {
  draft: { label: "Draft", status: "draft" },
  published: { label: "Published", status: "dispatched" },
  modified: { label: "Modified", status: "warning" },
  publishing: { label: "Publishing", status: "in_transit" },
  failed: { label: "Publish failed", status: "exception" },
  recovering: { label: "Recovering", status: "critical" },
};

/** "Darnell Price" → "D. Price" (the chip truncates; the full name is the tooltip). */
export function shortName(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length < 2) return name.trim();
  return `${parts[0][0].toUpperCase()}. ${parts[parts.length - 1]}`;
}

const EXPIRY_LABEL: Record<string, string> = {
  cdl: "CDL",
  medical_card: "Med card",
  hazmat: "Hazmat",
  tanker: "Tanker",
};
/** Qualification expiries shown in the lane header when within 30 days. */
const EXPIRY_SOON_DAYS = 30;

/** R2.8: the asset subtype as words ("tank_wagon" → "tank wagon"). */
export function truckTypeText(type: string): string {
  return type.replace(/_/g, " ").trim();
}

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
    lane.truck_type
      ? `Truck ${lane.truck_id} (${truckTypeText(lane.truck_type)})`
      : `Truck ${lane.truck_id}`,
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
  /** Identity colour from `assignLaneIdentities` (falls back to the hash). */
  identity?: string;
}

export function LaneHeader({
  lane,
  etasUnavailable,
  identity,
}: LaneHeaderProps) {
  const api = useBoard();
  const truckRef = useRef<HTMLButtonElement>(null);
  const driverRef = useRef<HTMLButtonElement>(null);
  const locked = api.readOnly || api.laneLocked(lane.truck_id);
  const truckRoving = useRovingItem(focusKey.lane(lane.truck_id));
  const driverRoving = useRovingItem(focusKey.driverSlot(lane.truck_id));
  const retryRoving = useRovingItem(`retry:${lane.truck_id}`);
  const overLane = useDropTarget(truckRef, {
    target: { target: "lane", truckId: lane.truck_id },
    enabled: true,
    readOnly: locked,
  });
  const overDriver = useDropTarget(driverRef, {
    target: { target: "driver-slot", truckId: lane.truck_id },
    enabled: true,
    readOnly: locked,
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
  // Visible chip text: initials + surname so it fits (accessible name stays full).
  const driverChip = placingDriver
    ? driverLabel
    : suggested
      ? `Pair ${shortName(suggested.name)}`
      : driverName
        ? shortName(driverName)
        : "No driver";
  const noDriver = !placingDriver && !suggested && !driverName;
  const stripe = identity ?? identityFor(lane.truck_id).hex;
  const quals: { key: string; text: string; warn: boolean }[] = [];
  if (lane.driver?.tanker_endorsement)
    quals.push({ key: "tanker", text: "Tanker ✓", warn: false });
  if (lane.driver?.hazmat_endorsement)
    quals.push({ key: "hazmat", text: "Hazmat ✓", warn: false });
  const expiry = lane.driver?.nearest_expiry;
  if (expiry) {
    const days =
      (Date.parse(`${expiry.expires_on}T00:00:00Z`) -
        Date.parse(`${api.serviceDate}T00:00:00Z`)) /
      86_400_000;
    if (Number.isFinite(days) && days <= EXPIRY_SOON_DAYS)
      quals.push({
        key: "expiry",
        text: `${EXPIRY_LABEL[expiry.kind] ?? expiry.kind} ${days < 0 ? "expired" : "exp"} ${formatDate(`${expiry.expires_on}T12:00:00Z`, { timeZone: "UTC" })}`,
        warn: true,
      });
  }
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

  const retryable =
    !api.readOnly && (lane.state === "failed" || lane.state === "recovering");
  const late = api.isToday ? api.lateBy(lane.truck_id) : null;

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
      className={`sticky left-0 z-10 flex shrink-0 flex-col border-r border-slate-200 bg-white pl-3 pr-2 ${
        // Compact (72 px): 24 px truck and driver rows plus the gauge fit
        // without clipping the driver slot (R4.3, N5).
        compact ? "gap-0.5 py-0.5" : "gap-1 py-1"
      } ${matchClass(lookup)}`}
      style={{ width: HEADER_WIDTH }}
    >
      {/* Truck identity stripe (decorative; the name and avatar carry it). */}
      <span
        aria-hidden="true"
        data-identity={stripe}
        className="absolute inset-y-1 left-0 w-[5px] rounded-r"
        style={{ backgroundColor: stripe }}
      />
      <div className="flex min-w-0 items-center gap-1">
        <div role="rowheader" tabIndex={-1} className="min-w-0">
          <button
            ref={truckRef}
            type="button"
            {...truckRoving}
            aria-haspopup={placeLabel ? undefined : "menu"}
            aria-label={placeLabel ?? `Truck ${lane.truck_id} actions`}
            onClick={onTruck}
            className={`${compact ? "min-h-6" : "min-h-7"} inline-flex max-w-full items-center gap-1.5 rounded-md px-1 text-left text-sm font-semibold text-slate-900 outline-none hover:bg-slate-50 focus-visible:ring-2 focus-visible:ring-primary ${
              overLane || placeLabel ? "ring-2 ring-primary" : ""
            }`}
          >
            <span
              aria-hidden="true"
              className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[9px] font-bold text-white"
              style={{ backgroundColor: stripe }}
            >
              {initials(lane.truck_id)}
            </span>
            <span className="truncate">Truck {lane.truck_id}</span>
            {lane.truck_type && (
              <span className="truncate text-xs font-normal text-slate-600">
                {truckTypeText(lane.truck_type)}
              </span>
            )}
          </button>
        </div>
        <StatusBadge status={state.status} label={state.label} />
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
        className="flex min-w-0 shrink-0 items-center gap-1 overflow-hidden whitespace-nowrap"
      >
        <button
          ref={driverRef}
          type="button"
          {...driverRoving}
          aria-label={driverLabel}
          onClick={onDriver}
          title={driverLabel}
          className={`inline-flex min-h-6 min-w-0 max-w-40 shrink-0 items-center gap-1 rounded-md border px-1 text-left text-xs outline-none focus-visible:ring-2 focus-visible:ring-primary ${
            noDriver
              ? "border-red-300 bg-red-50 font-semibold text-red-800"
              : suggested && !placingDriver
                ? "border-primary text-brand-800"
                : "border-slate-300 text-slate-800"
          } ${overDriver || placingDriver ? "ring-2 ring-primary" : ""}`}
        >
          {noDriver ? (
            <TriangleAlert aria-hidden="true" className="h-3 w-3 shrink-0" />
          ) : (
            !placingDriver && (
              <IdentityAvatar
                id={suggested?.driver_id ?? lane.driver_id ?? ""}
                label={suggested?.name ?? driverName ?? ""}
                size="xs"
                className="h-4 w-4 text-[8px]"
              />
            )
          )}
          <span className="truncate">{driverChip}</span>
        </button>
        {quals.map((q) => (
          <span
            key={q.key}
            className={`shrink-0 text-[11px] ${q.warn ? "font-medium text-amber-800" : "text-slate-600"}`}
          >
            {q.text}
          </span>
        ))}
        {hos !== null && (
          <span className="text-[11px] text-gray-600">
            HOS {formatNumber(hos, { decimals: 1 })} h
          </span>
        )}
        {retryable && (
          <button
            type="button"
            {...retryRoving}
            aria-label={`Retry publish on Truck ${lane.truck_id}`}
            onClick={() => api.retryPublish(lane.truck_id)}
            className="min-h-6 rounded-md border border-error px-1.5 text-[11px] font-medium text-error-dark outline-none hover:bg-error-light focus-visible:ring-2 focus-visible:ring-primary"
          >
            Retry
          </button>
        )}
        {late !== null && (
          <span className="text-[11px] font-medium text-error-dark">
            Running late +{late} min
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
        // A row may only own cells (axe aria-required-children).
        <div role="gridcell" tabIndex={-1}>
          <CompartmentGauge
            compartments={lane.compartments}
            allocations={gaugeLoad?.allocations ?? []}
            preview={preview}
            compact={compact}
          />
        </div>
      )}
    </div>
  );
}

export default LaneHeader;
