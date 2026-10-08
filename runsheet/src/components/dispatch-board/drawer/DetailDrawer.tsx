/**
 * Detail drawer for a lane, load or stop (design K14.1; R5.5, R5.6, R8.7,
 * R9.3, R9.6, R10.5, R17.1, R22.3). A labelled complementary panel on the
 * right of the board with four APG tabs: Checks, Compartments, Map and
 * History. Escape or the close button closes it; focus goes to its heading
 * when it opens and back to where it was opened from when it closes.
 */
import { X } from "lucide-react";
import { type KeyboardEvent, useEffect, useId, useRef } from "react";
import { type DrawerTab, type DrawerTarget, useBoard } from "../BoardContext";
import { LANE_STATE_LABEL } from "../grid/LaneHeader";
import { LaneRouteMap } from "../map/LaneRouteMap";
import type { TruckPosition } from "../map/runningLate";
import { ChecksTab } from "./ChecksTab";
import { CompartmentsTab } from "./CompartmentsTab";
import { HistoryTab } from "./HistoryTab";
import type { TerminalIndex } from "./useTerminalIndex";

const TABS: { id: DrawerTab; label: string }[] = [
  { id: "checks", label: "Checks" },
  { id: "compartments", label: "Compartments" },
  { id: "map", label: "Map" },
  { id: "history", label: "History" },
];

export interface DetailDrawerProps {
  target: DrawerTarget;
  onTab: (tab: DrawerTab) => void;
  onClose: () => void;
  terminals: TerminalIndex;
  positions: Record<string, TruckPosition>;
  /**
   * Stacked layout (< 1024 px, R20.2): a full-screen modal sheet. The board
   * behind it is made inert by the container.
   */
  fullScreen?: boolean;
}

export function DetailDrawer({
  target,
  onTab,
  onClose,
  terminals,
  positions,
  fullScreen = false,
}: DetailDrawerProps) {
  const api = useBoard();
  const titleId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const tabRefs = useRef<Partial<Record<DrawerTab, HTMLButtonElement | null>>>(
    {},
  );
  const tab = target.tab ?? "checks";
  const lane = api.lanesById[target.truckId];

  // Focus the heading on open and on a new target; return focus on close.
  const opener = useRef<Element | null>(null);
  useEffect(() => {
    opener.current ??= document.activeElement;
    headingRef.current?.focus();
  }, [target.truckId, target.loadId, target.orderId]);
  useEffect(
    () => () => {
      const el = opener.current as HTMLElement | null;
      if (el?.isConnected) el.focus();
    },
    [],
  );

  const onTabKey = (e: KeyboardEvent) => {
    const i = TABS.findIndex((t) => t.id === tab);
    let next: DrawerTab | null = null;
    if (e.key === "ArrowRight") next = TABS[(i + 1) % TABS.length].id;
    else if (e.key === "ArrowLeft")
      next = TABS[(i - 1 + TABS.length) % TABS.length].id;
    else if (e.key === "Home") next = TABS[0].id;
    else if (e.key === "End") next = TABS[TABS.length - 1].id;
    if (!next) return;
    e.preventDefault();
    onTab(next);
    tabRefs.current[next]?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
    }
  };

  const loadIndex = lane
    ? lane.loads.findIndex((l) => l.load_id === target.loadId)
    : -1;
  const subtitle = target.orderId
    ? `Order ${target.orderId}`
    : loadIndex >= 0
      ? `Load ${loadIndex + 1}`
      : "Whole truck";
  const publishable =
    lane && (lane.state === "draft" || lane.state === "modified");

  // A dialog role isn't allowed on <aside>, so the full-screen sheet is a div.
  const Panel = fullScreen ? "div" : "aside";
  return (
    <Panel
      aria-labelledby={titleId}
      onKeyDown={onKeyDown}
      {...(fullScreen ? { role: "dialog", "aria-modal": true } : {})}
      data-testid="detail-drawer"
      className={
        fullScreen
          ? "fixed inset-0 z-40 flex w-full flex-col bg-white"
          : "flex w-[26rem] max-w-full shrink-0 flex-col border-l border-gray-200 bg-white"
      }
    >
      <div className="flex items-start gap-2 border-b border-gray-200 px-3 py-2">
        <div className="min-w-0 flex-1">
          <h2
            id={titleId}
            ref={headingRef}
            tabIndex={-1}
            className="text-base font-semibold text-gray-900 outline-none"
          >
            Truck {target.truckId} details
          </h2>
          <p className="text-xs text-gray-600">
            {subtitle}
            {lane ? ` · ${LANE_STATE_LABEL[lane.state].label}` : ""}
          </p>
        </div>
        {publishable && !api.readOnly && (
          <button
            type="button"
            onClick={() => api.openPublish([target.truckId])}
            className="min-h-8 rounded-md bg-primary px-2 text-xs font-medium text-white"
          >
            Publish lane
          </button>
        )}
        <button
          type="button"
          aria-label="Close details"
          onClick={onClose}
          className="inline-flex min-h-8 min-w-8 items-center justify-center rounded-md text-gray-600 hover:bg-gray-100"
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
      {!lane ? (
        <p className="p-3 text-sm text-gray-600">
          Truck {target.truckId} is no longer on the board.
        </p>
      ) : (
        <>
          <div
            role="tablist"
            aria-label="Details"
            onKeyDown={onTabKey}
            className="flex border-b border-gray-200 px-2"
          >
            {TABS.map((t) => (
              <button
                key={t.id}
                ref={(el) => {
                  tabRefs.current[t.id] = el;
                }}
                id={`${titleId}-tab-${t.id}`}
                type="button"
                role="tab"
                aria-selected={tab === t.id}
                aria-controls={`${titleId}-panel`}
                tabIndex={tab === t.id ? 0 : -1}
                onClick={() => onTab(t.id)}
                className={`min-h-10 flex-1 border-b-2 px-2 text-xs font-medium ${
                  tab === t.id
                    ? "border-primary text-gray-900"
                    : "border-transparent text-gray-600 hover:text-gray-900"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div
            id={`${titleId}-panel`}
            role="tabpanel"
            aria-labelledby={`${titleId}-tab-${tab}`}
            className="min-h-0 flex-1 overflow-y-auto p-3"
          >
            {tab === "checks" && (
              <ChecksTab lane={lane} target={target} onTab={onTab} />
            )}
            {tab === "compartments" && (
              <CompartmentsTab
                lane={lane}
                target={target}
                terminals={terminals}
              />
            )}
            {tab === "map" && (
              <LaneRouteMap
                lanes={[lane]}
                loadId={target.orderId ? null : (target.loadId ?? null)}
                selectedOrderIds={
                  new Set(
                    api.state.selection
                      .filter((k) => k.startsWith("stop:"))
                      .map((k) => k.slice(5)),
                  )
                }
                onSelectStop={(orderId) => api.selectStops([orderId])}
                positions={positions}
                terminals={terminals.coords}
                label={`Route map for Truck ${lane.truck_id}`}
                className="h-80"
              />
            )}
            {tab === "history" && (
              <HistoryTab
                serviceDate={api.serviceDate}
                truckId={lane.truck_id}
                version={lane.version}
                timeZone={api.timezone}
              />
            )}
          </div>
        </>
      )}
    </Panel>
  );
}

export default DetailDrawer;
