/**
 * Driver tray (R3.4, R6.1): every tenant driver with status, pairing,
 * qualification summary and, on today only, HOS remaining. Drivers who can't
 * be dispatched stay listed with the reason. A driver chip is dragged by its
 * grip onto a driver slot, or selected (Place mode) and placed, or paired
 * from its menu ("Pair with truck…").
 */

import { GripVertical } from "lucide-react";
import { type KeyboardEvent, type MouseEvent, useRef } from "react";
import { number as fmtNumber } from "../../../lib/format";
import type {
  DriverSummary,
  QualificationExpiry,
} from "../../../services/dispatchBoardApi";
import { focusKey, useBoard } from "../BoardContext";
import { driverMatch, matchClass, matchSuffix } from "../boardMatch";
import { useDraggableItem } from "../dnd/adapter";
import { previewLabel } from "../dnd/dragData";
import {
  listKeyDown,
  RovingContext,
  useRovingItem,
  useRovingState,
} from "../keyboard/roving";
import { driverMenu } from "../menus";
import { todayIn } from "../viewState";

/** HOS figure in hours from the snapshot's `{availability, value}` shape. */
export function hosHours(
  hos: Record<string, unknown> | null | undefined,
  key: "remaining_drive_time" | "remaining_on_duty_window",
): number | null {
  const fig = hos?.[key] as
    | { availability?: string; value?: number | null }
    | undefined;
  if (!fig || fig.availability !== "available") return null;
  return typeof fig.value === "number" ? fig.value : null;
}

export function reasonText(code: string): string {
  return code.split(":")[0].replace(/_/g, " ").trim();
}

const EXPIRY_KIND: Record<QualificationExpiry["kind"], string> = {
  cdl: "CDL",
  medical_card: "Medical card",
  hazmat: "HAZMAT endorsement",
  tanker: "Tanker endorsement",
};

/**
 * R3.4: "Medical card expires 2026-11-02" (or "expired" when the date is
 * before `today`, an ISO date). Dates are compared as ISO strings.
 */
export function expiryText(
  expiry: QualificationExpiry | null | undefined,
  today: string,
): string | null {
  if (!expiry) return null;
  const verb = expiry.expires_on < today ? "expired" : "expires";
  return `${EXPIRY_KIND[expiry.kind]} ${verb} ${expiry.expires_on}`;
}

/** Text lines of a driver chip (also its accessible name). */
export function driverText(
  d: DriverSummary,
  isToday: boolean,
  today: string,
): string[] {
  const parts = [d.name ?? `Driver ${d.driver_id}`];
  parts.push(d.status ? d.status.replace(/_/g, " ") : "status unknown");
  parts.push(
    d.paired_truck_id ? `on Truck ${d.paired_truck_id}` : "not paired",
  );
  const qual: string[] = [];
  if (d.cdl_class) qual.push(`CDL ${d.cdl_class}`);
  if (d.hazmat_endorsement) qual.push("HAZMAT");
  if (d.tanker_endorsement) qual.push("Tanker");
  if (qual.length) parts.push(qual.join(", "));
  const expiry = expiryText(d.nearest_expiry, today);
  if (expiry) parts.push(expiry);
  if (isToday) {
    const drive = hosHours(d.hos, "remaining_drive_time");
    if (drive !== null)
      parts.push(`${fmtNumber(drive, { decimals: 1 })} h driving left`);
  }
  if (d.eligible === false) {
    const reasons = d.ineligible_reasons.map(reasonText).filter(Boolean);
    parts.push(
      `can't be dispatched${reasons.length ? `: ${reasons.join(", ")}` : ""}`,
    );
  }
  return parts;
}

function DriverChip({ driver }: { driver: DriverSummary }) {
  const api = useBoard();
  const ref = useRef<HTMLDivElement>(null);
  const grip = useRef<HTMLSpanElement>(null);
  const key = focusKey.driver(driver.driver_id);
  const roving = useRovingItem(key);
  const item = { kind: "driver" as const, ids: [driver.driver_id] };
  const dragging = useDraggableItem(ref, {
    item,
    handleRef: grip,
    enabled: !api.readOnly,
    previewLabel: () => previewLabel(item, null),
  });
  const selected = api.isSelected("driver", driver.driver_id);
  const state = driverMatch(driver, api.view.search);
  const text = driverText(driver, api.isToday, todayIn(api.timezone));
  const ineligible = driver.eligible === false;

  const openMenu = () =>
    api.showMenu({
      label: `${driver.name ?? driver.driver_id} actions`,
      items: driverMenu(api, driver.driver_id),
      anchor: ref.current,
    });

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      api.select(item);
    } else if ((e.key === "F10" && e.shiftKey) || e.key === "ContextMenu") {
      e.preventDefault();
      openMenu();
    }
  };

  return (
    <div
      ref={ref}
      role="option"
      aria-selected={selected}
      aria-label={`${text.join(", ")}${matchSuffix(state)}`}
      {...roving}
      tabIndex={roving.tabIndex}
      onClick={(_e: MouseEvent) => api.select(item)}
      onKeyDown={onKeyDown}
      onContextMenu={(e) => {
        e.preventDefault();
        openMenu();
      }}
      className={`flex gap-1 rounded-md border bg-white p-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-primary ${
        selected ? "border-primary bg-primary-soft" : "border-gray-200"
      } ${dragging ? "opacity-50" : ""} ${matchClass(state)}`}
    >
      {!api.readOnly && (
        <span
          ref={grip}
          aria-hidden="true"
          data-drag-handle=""
          className="flex min-h-6 min-w-6 cursor-grab touch-none items-center justify-center text-gray-400 pointer-coarse:min-h-11 pointer-coarse:min-w-11"
        >
          <GripVertical className="h-4 w-4" />
        </span>
      )}
      <div aria-hidden="true" className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate font-medium text-gray-900">{text[0]}</span>
          <span className="text-xs text-gray-600">{text[1]}</span>
        </div>
        <div className="text-xs text-gray-600">{text.slice(2).join(" · ")}</div>
        {ineligible && (
          <div className="mt-1 text-xs font-medium text-error-dark">
            Not eligible
          </div>
        )}
      </div>
    </div>
  );
}

export function DriverTray() {
  const api = useBoard();
  const ref = useRef<HTMLDivElement>(null);
  const roving = useRovingState(ref);
  const drivers = api.snapshot.trays.drivers;
  return (
    <section aria-label="Driver tray" className="flex min-h-0 flex-1 flex-col">
      <h2 className="pb-2 text-sm font-semibold text-gray-900">
        Drivers ({drivers.length})
      </h2>
      {drivers.length === 0 ? (
        <p className="text-sm text-gray-600">No drivers.</p>
      ) : (
        <RovingContext.Provider value={roving}>
          <div
            ref={ref}
            role="listbox"
            aria-label="Drivers"
            onKeyDown={(e) => listKeyDown(e, ref.current, roving)}
            className="min-h-0 flex-1 space-y-2 overflow-auto pr-1"
          >
            {drivers.map((d) => (
              <DriverChip key={d.driver_id} driver={d} />
            ))}
          </div>
        </RovingContext.Provider>
      )}
    </section>
  );
}

export default DriverTray;
