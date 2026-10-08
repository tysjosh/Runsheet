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
    <div className="p-6 space-y-4">
      <div
        role="tablist"
        aria-label="Margin sections"
        className="flex flex-wrap gap-2 border-b border-gray-200"
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
              className={`px-3 py-2 text-sm font-medium border-b-2 -mb-px focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 ${
                selected
                  ? "border-primary text-primary"
                  : "border-transparent text-gray-600 hover:text-gray-800"
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`margin-panel-${active}`}
        aria-labelledby={`margin-tab-${active}`}
        className="focus:outline-none"
      >
        {active === "records" && <MarginRecordsPage />}
        {active === "summary" && <MarginSummaryPanel />}
        {active === "alerts" && (
          <MarginAlertsPanel onChanged={onAlertsChanged} />
        )}
        {active === "cost-basis" && <CostBasisViewer />}
        {active === "cost-entries" && <CostEntriesPage />}
        {active === "settings" && (
          <div className="space-y-8">
            <MarginSettingsForm />
            <MarginRecomputePanel />
          </div>
        )}
      </div>
    </div>
  );
}
