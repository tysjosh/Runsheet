"use client";

import { lazy, Suspense, useState } from "react";
import { Package, Truck, Package as PackageIcon, User, Shield } from "lucide-react";
import ErrorBoundary from "./ErrorBoundary";
import LoadingSpinner from "./LoadingSpinner";
import ExpiryAlertWidget from "./compliance/ExpiryAlertWidget";
import type { Truck as TruckType } from "../types/api";

const FleetTracking = lazy(() => import("./FleetTracking"));
const ShipmentBoardView = lazy(() => import("../app/ops/page"));
const Inventory = lazy(() => import("./Inventory"));
const DriversPage = lazy(() => import("./compliance/DriversPage"));
const AssetCertificationsPage = lazy(() => import("./compliance/AssetCertificationsPage"));

interface FleetDashboardProps {
  selectedTruck: TruckType | null;
  onTruckSelect: (truck: TruckType) => void;
  mapView: React.ReactNode;
}

const TABS = [
  { id: "assets", label: "Asset Tracking", icon: Truck },
  { id: "shipments", label: "Orders", icon: Package },
  { id: "inventory", label: "Inventory", icon: PackageIcon },
  { id: "drivers", label: "Drivers", icon: User },
  { id: "certifications", label: "Certifications", icon: Shield },
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
      <div className="flex items-center gap-1 px-6 pt-4 pb-0" role="tablist" aria-label="Fleet dashboard views">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            aria-controls={`tabpanel-${tab.id}`}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-t-lg transition-colors focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-gray-500 ${
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
          <div className="flex flex-col gap-4 h-full p-6 pt-4 overflow-auto">
            {/* Expiry Alert Summary Widget */}
            <ErrorBoundary componentName="Expiry Alerts">
              <ExpiryAlertWidget
                onViewDrivers={() => setActiveTab("drivers")}
                onViewCertifications={() => setActiveTab("certifications")}
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
              <Suspense fallback={<LoadingSpinner message="Loading orders..." />}>
                <ShipmentBoardView />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}

        {activeTab === "inventory" && (
          <div className="h-full bg-white border-t border-gray-200 overflow-auto">
            <ErrorBoundary componentName="Inventory">
              <Suspense fallback={<LoadingSpinner message="Loading inventory..." />}>
                <Inventory />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}

        {activeTab === "drivers" && (
          <div className="h-full bg-white border-t border-gray-200 overflow-auto">
            <ErrorBoundary componentName="Drivers">
              <Suspense fallback={<LoadingSpinner message="Loading drivers..." />}>
                <DriversPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}

        {activeTab === "certifications" && (
          <div className="h-full bg-white border-t border-gray-200 overflow-auto">
            <ErrorBoundary componentName="Asset Certifications">
              <Suspense fallback={<LoadingSpinner message="Loading certifications..." />}>
                <AssetCertificationsPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}
      </div>
    </div>
  );
}
