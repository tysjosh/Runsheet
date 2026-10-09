"use client";

/**
 * MarginHub - the admin-only Margin tab of CommerceHub (margin-feed FR6.3).
 *
 * An accessible sub-tab list (`role="tablist"`, roving tabindex, Arrow /
 * Home / End keys). CommerceHub only renders this for a tenant `admin`
 * (`canSee("margin")`); the API refuses everyone else anyway.
 */
import { type KeyboardEvent, useRef, useState } from "react";
import CostBasisViewer from "./CostBasisViewer";
import CostEntriesPage from "./CostEntriesPage";
import MarginAlertsPanel from "./MarginAlertsPanel";
import MarginRecomputePanel from "./MarginRecomputePanel";
import MarginRecordsPage from "./MarginRecordsPage";
import MarginSettingsForm from "./MarginSettingsForm";
import MarginSummaryPanel from "./MarginSummaryPanel";
import { MarginToolbarSlot } from "./marginToolbarSlot";

export const MARGIN_SUB_TABS = [
  { id: "records", label: "Records" },
  { id: "summary", label: "Summary" },
  { id: "alerts", label: "Alerts" },
  { id: "cost-basis", label: "Cost basis" },
  { id: "cost-entries", label: "Cost entries" },
  { id: "settings", label: "Settings" },
] as const;

type SubTabId = (typeof MARGIN_SUB_TABS)[number]["id"];

export interface MarginHubProps {
  /** Called after an alert is resolved so the tab badge can refresh. */
  onAlertsChanged?: () => void;
}

export default function MarginHub({ onAlertsChanged }: MarginHubProps) {
  const [active, setActive] = useState<SubTabId>("records");
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const select = (index: number) => {
    const count = MARGIN_SUB_TABS.length;
    const tab = MARGIN_SUB_TABS[(index + count) % count];
    setActive(tab.id);
    tabRefs.current[tab.id]?.focus();
  };

  const onKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    const keys: Record<string, number> = {
      ArrowRight: index + 1,
      ArrowLeft: index - 1,
      Home: 0,
      End: MARGIN_SUB_TABS.length - 1,
    };
    if (event.key in keys) {
      event.preventDefault();
      select(keys[event.key]);
    }
  };

  return (
    <div className="flex h-full flex-col">
      {/* One 44 px row (task 3.4): sub-views as a segmented tab list, with
          the active view's toolbar at the end. */}
      <div className="flex h-11 shrink-0 items-center gap-2 border-b border-slate-200 px-4">
        <div
          role="tablist"
          aria-label="Margin sections"
          className="flex min-w-0 items-center gap-1 overflow-x-auto"
        >
          {MARGIN_SUB_TABS.map((tab, index) => {
            const selected = tab.id === active;
            return (
              <button
                key={tab.id}
                ref={(el) => {
                  tabRefs.current[tab.id] = el;
                }}
                type="button"
                role="tab"
                id={`margin-tab-${tab.id}`}
                aria-selected={selected}
                aria-controls={`margin-panel-${tab.id}`}
                tabIndex={selected ? 0 : -1}
                onClick={() => setActive(tab.id)}
                onKeyDown={(e) => onKeyDown(e, index)}
                className={`h-7 shrink-0 rounded-full border px-2.5 text-xs font-semibold focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
                  selected
                    ? "border-primary bg-primary-soft text-brand-800"
                    : "border-slate-300 bg-surface text-slate-700 hover:bg-slate-50"
                }`}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
        <div
          ref={setSlot}
          data-testid="margin-toolbar-slot"
          className="ml-auto flex shrink-0 items-center gap-2"
        />
      </div>
      <MarginToolbarSlot.Provider value={slot}>
        <div
          role="tabpanel"
          id={`margin-panel-${active}`}
          aria-labelledby={`margin-tab-${active}`}
          className="min-h-0 flex-1 overflow-auto focus:outline-none"
        >
          {active === "records" && (
            <div className="px-4 pb-4">
              <MarginRecordsPage />
            </div>
          )}
          {active === "summary" && (
            <div className="p-4">
              <MarginSummaryPanel />
            </div>
          )}
          {active === "alerts" && (
            <div className="p-4">
              <MarginAlertsPanel onChanged={onAlertsChanged} />
            </div>
          )}
          {active === "cost-basis" && (
            <div className="p-4">
              <CostBasisViewer />
            </div>
          )}
          {active === "cost-entries" && <CostEntriesPage />}
          {active === "settings" && (
            <div className="space-y-8 p-4">
              <MarginSettingsForm />
              <MarginRecomputePanel />
            </div>
          )}
        </div>
      </MarginToolbarSlot.Provider>
    </div>
  );
}
