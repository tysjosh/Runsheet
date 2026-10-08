/**
 * Left panel: Orders, Drivers and Trucks trays as tabs (design K14.1). APG
 * tabs: Left/Right move between tabs, the active tab is the only one in the
 * Tab order.
 */
import { type KeyboardEvent, useRef, useState } from "react";
import { useBoard } from "../BoardContext";
import { DriverTray } from "./DriverTray";
import { OrderTray, type TraySort, TraySortSelect } from "./OrderTray";
import { TruckTray } from "./TruckTray";
import { trayOrders } from "./trayOrders";

type TrayTab = "orders" | "drivers" | "trucks";
const TABS: TrayTab[] = ["orders", "drivers", "trucks"];

export interface TrayPanelProps {
  plannedCount: number;
  onGoToOrders: () => void;
  /** `sheet`: inside the stacked layout's bottom sheet (R20.2), full width. */
  variant?: "panel" | "sheet";
}

export function TrayPanel({
  plannedCount,
  onGoToOrders,
  variant = "panel",
}: TrayPanelProps) {
  const api = useBoard();
  const [tab, setTab] = useState<TrayTab>("orders");
  const [sort, setSort] = useState<TraySort>("priority");
  const refs = useRef<Record<TrayTab, HTMLButtonElement | null>>({
    orders: null,
    drivers: null,
    trucks: null,
  });
  const { trays } = api.snapshot;
  const label: Record<TrayTab, string> = {
    orders: `Orders (${trayOrders(trays.orders, api.lanesById).length})`,
    drivers: `Drivers (${trays.drivers.length})`,
    trucks: `Trucks (${trays.trucks.length})`,
  };

  const onKeyDown = (e: KeyboardEvent) => {
    const i = TABS.indexOf(tab);
    let next: TrayTab | null = null;
    if (e.key === "ArrowRight") next = TABS[(i + 1) % TABS.length];
    else if (e.key === "ArrowLeft")
      next = TABS[(i - 1 + TABS.length) % TABS.length];
    else if (e.key === "Home") next = TABS[0];
    else if (e.key === "End") next = TABS[TABS.length - 1];
    if (!next) return;
    e.preventDefault();
    setTab(next);
    refs.current[next]?.focus();
  };

  return (
    <section
      aria-label="Trays"
      className={
        variant === "sheet"
          ? "flex min-h-[50vh] flex-1 flex-col bg-white"
          : "flex w-80 shrink-0 flex-col border-r border-gray-200 bg-white"
      }
    >
      {/* One row: the tray tabs, then (Orders only) the Sort control. The
          tray's own "Orders (n)" heading repeated the tab, so it's gone. */}
      <div className="flex items-center gap-1 border-b border-gray-200 pr-2">
        <div
          role="tablist"
          aria-label="Trays"
          onKeyDown={onKeyDown}
          className="flex min-w-0 flex-1 px-2"
        >
          {TABS.map((t) => (
            <button
              key={t}
              ref={(el) => {
                refs.current[t] = el;
              }}
              id={`tray-tab-${t}`}
              type="button"
              role="tab"
              aria-selected={tab === t}
              aria-controls={`tray-panel-${t}`}
              tabIndex={tab === t ? 0 : -1}
              onClick={() => setTab(t)}
              className={`min-h-10 flex-1 whitespace-nowrap border-b-2 px-1.5 text-xs font-medium ${
                tab === t
                  ? "border-primary text-gray-900"
                  : "border-transparent text-gray-600 hover:text-gray-900"
              }`}
            >
              {label[t]}
            </button>
          ))}
        </div>
        {tab === "orders" && <TraySortSelect value={sort} onChange={setSort} />}
      </div>
      <div
        id={`tray-panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`tray-tab-${tab}`}
        className="flex min-h-0 flex-1 flex-col p-3"
      >
        {tab === "orders" && (
          <OrderTray
            plannedCount={plannedCount}
            onGoToOrders={onGoToOrders}
            sort={sort}
          />
        )}
        {tab === "drivers" && <DriverTray />}
        {tab === "trucks" && <TruckTray />}
      </div>
    </section>
  );
}

export default TrayPanel;
