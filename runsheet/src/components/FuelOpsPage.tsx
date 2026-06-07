"use client";
import { Building2, Fuel } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const FuelDashboard = lazy(() => import("../app/ops/fuel/page"));
const SourcingPage = lazy(() => import("./ops/SourcingPage"));

const TABS: Tab[] = [
  {
    id: "stations",
    label: "Fuel Stations",
    icon: <Fuel className="w-4 h-4" />,
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
        subtitle="Monitor stations and source supply"
        icon={<Fuel className="w-5 h-5" />}
      />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {activeTab === "stations" && <FuelDashboard />}
          {activeTab === "sourcing" && <SourcingPage />}
        </Suspense>
      </div>
    </div>
  );
}
