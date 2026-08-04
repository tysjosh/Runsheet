"use client";
import { Activity, BarChart3, Building2, Fuel } from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import { canSee, visibleByCanSee } from "../config/modules";
import { getCurrentUserRoles } from "../utils/auth";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const FuelDashboard = lazy(() => import("./ops/FuelDashboardView"));
const SourcingPage = lazy(() => import("./ops/SourcingPage"));
const KFactorCalibrationPage = lazy(
  () => import("./compliance/KFactorCalibrationPage"),
);

// Flattened to one tab set so the hub doesn't stack a second header + tab bar
// on top of the embedded dashboard. Stations/Consumption drive the embedded
// fuel dashboard's view; Sourcing and K-Factor are their own pages.
// Exported for the registry drift guard in `config/modules.test.ts`.
export const TABS: Tab[] = [
  {
    id: "stations",
    label: "Fuel Stations",
    icon: <Fuel className="w-4 h-4" />,
  },
  {
    id: "efficiency",
    label: "Consumption",
    icon: <BarChart3 className="w-4 h-4" />,
  },
  {
    id: "kfactor",
    label: "K-Factor",
    icon: <Activity className="w-4 h-4" />,
  },
  {
    id: "sourcing",
    label: "Sourcing",
    icon: <Building2 className="w-4 h-4" />,
  },
];

type TabId = string;

export default function FuelOpsPage() {
  const [activeTab, setActiveTab] = useState<TabId>("stations");
  // `null` until resolved; `canSee` treats that as no roles.
  const [roles, setRoles] = useState<readonly string[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const r = await getCurrentUserRoles();
      if (!cancelled) setRoles(r);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const visibleTabs = visibleByCanSee(TABS, { roles });
  const effectiveTab =
    visibleTabs.some((t) => t.id === activeTab) || visibleTabs.length === 0
      ? activeTab
      : visibleTabs[0].id;
  const shows = (id: string) => effectiveTab === id && canSee(id, { roles });

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="Fuel Operations"
        subtitle="Monitor stations, consumption, and source supply"
        icon={<Fuel className="w-5 h-5" />}
      />
      <TabNavigation
        tabs={visibleTabs}
        activeTab={effectiveTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {(shows("stations") || shows("efficiency")) && (
            <FuelDashboard
              embedded
              view={effectiveTab as "stations" | "efficiency"}
            />
          )}
          {shows("sourcing") && <SourcingPage />}
          {shows("kfactor") && <KFactorCalibrationPage />}
        </Suspense>
      </div>
    </div>
  );
}
