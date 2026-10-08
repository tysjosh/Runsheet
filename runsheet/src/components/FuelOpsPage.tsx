"use client";

import { Activity, BarChart3, Building2, Fuel } from "lucide-react";
import { lazy, Suspense } from "react";
import LoadingSpinner from "./LoadingSpinner";
import { useHubTabs } from "./shell/useHubTabs";
import { PageChromeProvider, PageHeader, type Tab, TabPanel } from "./ui";

const FuelDashboard = lazy(() => import("./ops/FuelDashboardView"));
const SourcingPage = lazy(() => import("./ops/SourcingPage"));
const KFactorCalibrationPage = lazy(
  () => import("./compliance/KFactorCalibrationPage"),
);

// One tab set in the title row. Stations/Consumption drive the embedded fuel
// dashboard's view; Sourcing and K-Factor are their own pages.
// Exported for the registry drift guard in `config/modules.test.ts`.
export const TABS: Tab[] = [
  {
    id: "stations",
    label: "Stations",
    icon: <Fuel className="w-4 h-4" />,
  },
  {
    id: "efficiency",
    label: "Consumption",
    icon: <BarChart3 className="w-4 h-4" />,
  },
  {
    id: "sourcing",
    label: "Sourcing",
    icon: <Building2 className="w-4 h-4" />,
  },
  {
    id: "kfactor",
    label: "K-Factor",
    icon: <Activity className="w-4 h-4" />,
  },
];

export default function FuelOpsPage() {
  const { tabs, active, setActive, shows } = useHubTabs(TABS, {
    aliases: { consumption: "efficiency" },
  });
  return (
    <PageChromeProvider>
      <div className="flex flex-col h-full">
        <PageHeader
          host
          title="Fuel"
          help="Monitor stations, consumption, and source supply"
          tabs={tabs}
          tab={active}
          onTabChange={setActive}
          tabIdBase="fuel"
        />
        <TabPanel idBase="fuel" value={active} className="flex-1 overflow-auto">
          <Suspense fallback={<LoadingSpinner message="Loading..." />}>
            {(shows("stations") || shows("efficiency")) && (
              <FuelDashboard
                embedded
                view={active as "stations" | "efficiency"}
              />
            )}
            {shows("sourcing") && <SourcingPage />}
            {shows("kfactor") && <KFactorCalibrationPage />}
          </Suspense>
        </TabPanel>
      </div>
    </PageChromeProvider>
  );
}
