"use client";
import { CalendarClock, Droplets } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const SchedulingJobBoard = lazy(() => import("../app/ops/scheduling/page"));
const FuelDistributionPage = lazy(() => import("./ops/FuelDistributionPage"));

const TABS: Tab[] = [
  {
    id: "scheduling",
    label: "Scheduling",
    icon: <CalendarClock className="w-4 h-4" />,
  },
  {
    id: "distribution",
    label: "Fuel Distribution",
    icon: <Droplets className="w-4 h-4" />,
  },
];

type TabId = string;

export default function DispatchPage() {
  const [activeTab, setActiveTab] = useState<TabId>("scheduling");

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="Dispatch"
        subtitle="Schedule jobs and plan fuel distribution runs"
        icon={<CalendarClock className="w-5 h-5" />}
      />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {activeTab === "scheduling" && <SchedulingJobBoard />}
          {activeTab === "distribution" && <FuelDistributionPage />}
        </Suspense>
      </div>
    </div>
  );
}
