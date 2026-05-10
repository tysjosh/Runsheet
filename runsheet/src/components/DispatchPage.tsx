"use client";
import { useState, lazy, Suspense } from "react";
import { CalendarClock, Droplets, Users } from "lucide-react";
import LoadingSpinner from "./LoadingSpinner";

const SchedulingJobBoard = lazy(() => import("../app/ops/scheduling/page"));
const FuelDistributionPage = lazy(() => import("./ops/FuelDistributionPage"));
const DriverUtilizationList = lazy(() => import("./ops/DriverUtilizationList"));

const TABS = [
  { id: "scheduling", label: "Scheduling", icon: CalendarClock },
  { id: "distribution", label: "Fuel Distribution", icon: Droplets },
  { id: "drivers", label: "Drivers", icon: Users },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function DispatchPage() {
  const [activeTab, setActiveTab] = useState<TabId>("scheduling");

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
        <h1 className="text-xl font-semibold text-gray-900">Dispatch</h1>
      </div>
      <div className="flex border-b border-gray-200 px-6">
        {TABS.map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition-colors ${
                isActive
                  ? "border-gray-900 text-gray-900"
                  : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
              }`}
            >
              <Icon className="w-4 h-4" />
              {tab.label}
            </button>
          );
        })}
      </div>
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {activeTab === "scheduling" && <SchedulingJobBoard />}
          {activeTab === "distribution" && <FuelDistributionPage />}
          {activeTab === "drivers" && <DriverUtilizationList drivers={[]} statusFilter="" onStatusFilterChange={() => {}} />}
        </Suspense>
      </div>
    </div>
  );
}
