"use client";

import { lazy, Suspense, useState } from "react";
import { Package, Truck } from "lucide-react";
import ErrorBoundary from "./ErrorBoundary";
import LoadingSpinner from "./LoadingSpinner";
import type { Truck as TruckType } from "../types/api";

const FleetTracking = lazy(() => import("./FleetTracking"));
const ShipmentBoardView = lazy(() => import("../app/ops/page"));

interface FleetDashboardProps {
  selectedTruck: TruckType | null;
  onTruckSelect: (truck: TruckType) => void;
  mapView: React.ReactNode;
}

const TABS = [
  { id: "assets", label: "Asset Tracking", icon: Truck },
  { id: "shipments", label: "Shipments", icon: Package },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function FleetDashboard({
  selectedTruck,
  onTruckSelect,
  mapView,
}: FleetDashboardProps) {
  const [activeTab, setActiveTab] = useState<TabId>("assets");

  return (
    <div className="flex-1 flex flex-col h-full bg-gray-50">
      {/* Tab bar */}
      <div className="flex items-center gap-1 px-6 pt-4 pb-0">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${
              activeTab === tab.id
                ? "bg-white text-[#232323] border border-gray-200 border-b-white -mb-px z-10"
                : "text-gray-500 hover:text-gray-700 hover:bg-gray-100"
            }`}
          >
            <tab.icon className="w-4 h-4" />
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {activeTab === "assets" && (
          <div className="flex gap-6 h-full p-6 pt-4">
            <div className="w-1/2 bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
              <ErrorBoundary componentName="Fleet Tracking">
                <Suspense fallback={<LoadingSpinner message="Loading..." />}>
                  <FleetTracking onTruckSelect={onTruckSelect} />
                </Suspense>
              </ErrorBoundary>
            </div>
            <div className="w-1/2 bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
              {mapView}
            </div>
          </div>
        )}

        {activeTab === "shipments" && (
          <div className="h-full bg-white border-t border-gray-200">
            <ErrorBoundary componentName="Shipments">
              <Suspense fallback={<LoadingSpinner message="Loading shipments..." />}>
                <ShipmentBoardView />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}
      </div>
    </div>
  );
}
