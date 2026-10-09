"use client";

/**
 * Fleet hub: Trucks · Drivers · Inventory (UI revamp IA, audit §b).
 *
 * The tab is in the URL (`?tab=`). The former Orders tab (a duplicate of the
 * Orders list, now in the sidebar) is gone; Drivers moved here from its own
 * nav item (`/dashboard/fleet?tab=drivers` redirects to `?tab=drivers`).
 */
import { Package, Truck, Users } from "lucide-react";
import { lazy, Suspense } from "react";
import { canSee } from "../config/modules";
import type { Truck as TruckType } from "../types/api";
import ExpiryAlertWidget from "./compliance/ExpiryAlertWidget";
import ErrorBoundary from "./ErrorBoundary";
import LoadingSpinner from "./LoadingSpinner";
import { useHubTabs } from "./shell/useHubTabs";
import { PageChromeProvider, PageHeader, type Tab, TabPanel } from "./ui";

const FleetTracking = lazy(() => import("./FleetTracking"));
const Inventory = lazy(() => import("./Inventory"));
const DriversHub = lazy(() => import("./DriversHub"));

interface FleetDashboardProps {
  selectedTruck: TruckType | null;
  onTruckSelect: (truck: TruckType) => void;
  mapView: React.ReactNode;
  /**
   * Asset id from `?asset=` on /dashboard/fleet (the canonical asset
   * destination). Forwarded to the tracking table so the referenced asset is
   * actually selected.
   */
  focusAssetId?: string | null;
  /**
   * Navigate to a top-level dashboard module. Kept for callers; the expiry
   * chip now links to Compliance directly.
   */
  onNavigate?: (item: string) => void;
}

// Exported for the registry drift guard in `config/modules.test.ts`.
export const TABS: Tab[] = [
  { id: "trucks", label: "Trucks", icon: <Truck className="w-4 h-4" /> },
  { id: "drivers", label: "Drivers", icon: <Users className="w-4 h-4" /> },
  {
    id: "inventory",
    label: "Inventory",
    icon: <Package className="w-4 h-4" />,
  },
];

/** Registry gate per tab (`trucks` and `inventory` ride on `fleet`). */
const GATE: Record<string, string> = {
  trucks: "fleet",
  drivers: "drivers",
  inventory: "fleet",
};

export default function FleetDashboard({
  selectedTruck: _selectedTruck,
  onTruckSelect,
  mapView,
  focusAssetId,
}: FleetDashboardProps) {
  const { tabs, active, setActive, shows } = useHubTabs(TABS, {
    aliases: { assets: "trucks" },
    visible: (tab, roles) => canSee(GATE[tab.id] ?? tab.id, { roles }),
  });
  return (
    <PageChromeProvider>
      <div className="flex-1 flex flex-col h-full bg-surface">
        <PageHeader
          host
          title="Fleet"
          tabs={tabs}
          tab={active}
          onTabChange={setActive}
          tabIdBase="fleet"
          counts={
            // Compliance expiry as one title-row chip linking to Compliance
            // (design §6 rule 4); hidden when everything is current.
            <ErrorBoundary componentName="Expiry Alerts">
              <ExpiryAlertWidget />
            </ErrorBoundary>
          }
        />
        <TabPanel
          idBase="fleet"
          value={active}
          className="flex-1 min-h-0 overflow-hidden"
        >
          {shows("trucks") && (
            // Table and map side by side only on wide screens (≥ 1536 px); at
            // 1280–1440 the table gets the full width (its toolbar needs it
            // for the status chips) and the map follows below.
            <div className="flex h-full min-h-0 flex-col overflow-auto 2xl:flex-row 2xl:overflow-hidden">
              <div className="h-full min-h-[480px] w-full shrink-0 overflow-hidden border-slate-200 bg-surface 2xl:min-h-0 2xl:w-3/5 2xl:shrink 2xl:border-r">
                <ErrorBoundary componentName="Fleet Tracking">
                  <Suspense fallback={<LoadingSpinner message="Loading..." />}>
                    <FleetTracking
                      onTruckSelect={onTruckSelect}
                      focusAssetId={focusAssetId}
                    />
                  </Suspense>
                </ErrorBoundary>
              </div>
              <div className="min-h-[360px] w-full shrink-0 overflow-hidden border-t border-slate-200 bg-surface 2xl:min-h-0 2xl:w-2/5 2xl:shrink 2xl:border-t-0">
                {mapView}
              </div>
            </div>
          )}
          {shows("drivers") && (
            <div className="h-full bg-white overflow-hidden">
              <ErrorBoundary componentName="Drivers">
                <Suspense
                  fallback={<LoadingSpinner message="Loading drivers..." />}
                >
                  <DriversHub />
                </Suspense>
              </ErrorBoundary>
            </div>
          )}
          {shows("inventory") && (
            <div className="h-full bg-white overflow-auto">
              <ErrorBoundary componentName="Inventory">
                <Suspense
                  fallback={<LoadingSpinner message="Loading inventory..." />}
                >
                  <Inventory />
                </Suspense>
              </ErrorBoundary>
            </div>
          )}
        </TabPanel>
      </div>
    </PageChromeProvider>
  );
}
