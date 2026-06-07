"use client";

import { Package, Package as PackageIcon, Truck } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import type { Truck as TruckType } from "../types/api";
import ExpiryAlertWidget from "./compliance/ExpiryAlertWidget";
import ErrorBoundary from "./ErrorBoundary";
import LoadingSpinner from "./LoadingSpinner";
import { type Tab, TabNavigation } from "./ui";

const FleetTracking = lazy(() => import("./FleetTracking"));
const ShipmentBoardView = lazy(() => import("../app/ops/page"));
const Inventory = lazy(() => import("./Inventory"));

interface FleetDashboardProps {
  selectedTruck: TruckType | null;
  onTruckSelect: (truck: TruckType) => void;
  mapView: React.ReactNode;
  /**
   * Navigate to a top-level dashboard module (e.g. "drivers"). Wired from
   * the dashboard shell so in-page affordances like the expiry-alert
   * widget's "Driver Qualifications" row can jump to the Drivers hub.
   */
  onNavigate?: (item: string) => void;
}

const TABS: Tab[] = [
  {
    id: "assets",
    label: "Asset Tracking",
    icon: <Truck className="w-4 h-4" />,
  },
  { id: "shipments", label: "Orders", icon: <Package className="w-4 h-4" /> },
  {
    id: "inventory",
    label: "Inventory",
    icon: <PackageIcon className="w-4 h-4" />,
  },
];

type TabId = string;

export default function FleetDashboard({
  selectedTruck,
  onTruckSelect,
  mapView,
  onNavigate,
}: FleetDashboardProps) {
  const [activeTab, setActiveTab] = useState<TabId>("assets");

  return (
    <div className="flex-1 flex flex-col h-full bg-gray-50">
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
      />

      {/* Tab content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {activeTab === "assets" && (
          <div className="flex flex-col gap-4 h-full p-6 pt-4 overflow-auto">
            {/* Expiry Alert Summary Widget */}
            <ErrorBoundary componentName="Expiry Alerts">
              <ExpiryAlertWidget
                onViewCertifications={
                  onNavigate ? () => onNavigate("compliance") : undefined
                }
                onViewDrivers={
                  onNavigate ? () => onNavigate("drivers") : undefined
                }
              />
            </ErrorBoundary>

            {/* Fleet Tracking + Map */}
            <div className="flex gap-6 flex-1 min-h-0">
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
          </div>
        )}

        {activeTab === "shipments" && (
          <div className="h-full bg-white border-t border-gray-200">
            <ErrorBoundary componentName="Orders">
              <Suspense
                fallback={<LoadingSpinner message="Loading orders..." />}
              >
                <ShipmentBoardView />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}

        {activeTab === "inventory" && (
          <div className="h-full bg-white border-t border-gray-200 overflow-auto">
            <ErrorBoundary componentName="Inventory">
              <Suspense
                fallback={<LoadingSpinner message="Loading inventory..." />}
              >
                <Inventory />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}
      </div>
    </div>
  );
}
