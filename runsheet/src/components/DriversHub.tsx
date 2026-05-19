"use client";

import { Activity, FileText } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const DriverUtilizationView = lazy(
  () => import("./drivers/DriverUtilizationView"),
);
const DriverQualificationsView = lazy(
  () => import("./drivers/DriverQualificationsView"),
);

const TABS: Tab[] = [
  {
    id: "utilization",
    label: "Utilization",
    icon: <Activity className="w-4 h-4" />,
  },
  {
    id: "qualifications",
    label: "Qualifications",
    icon: <FileText className="w-4 h-4" />,
  },
];

type TabId = "utilization" | "qualifications";

export default function DriversHub() {
  const [activeTab, setActiveTab] = useState<TabId>("utilization");

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="Drivers"
        subtitle="Manage driver utilization and qualifications"
      />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab as (tab: string) => void}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {activeTab === "utilization" && <DriverUtilizationView />}
          {activeTab === "qualifications" && <DriverQualificationsView />}
        </Suspense>
      </div>
    </div>
  );
}
