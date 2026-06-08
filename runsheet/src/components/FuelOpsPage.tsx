"use client";
import { Activity, BarChart3, Building2, Fuel } from "lucide-react";
import { lazy, Suspense, useState } from "react";
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
const TABS: Tab[] = [
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

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="Fuel Operations"
        subtitle="Monitor stations, consumption, and source supply"
        icon={<Fuel className="w-5 h-5" />}
      />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {(activeTab === "stations" || activeTab === "efficiency") && (
            <FuelDashboard
              embedded
              view={activeTab as "stations" | "efficiency"}
            />
          )}
          {activeTab === "sourcing" && <SourcingPage />}
          {activeTab === "kfactor" && <KFactorCalibrationPage />}
        </Suspense>
      </div>
    </div>
  );
}
