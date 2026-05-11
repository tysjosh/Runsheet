"use client";
import { useState, lazy, Suspense } from "react";
import { Fuel, Gauge, SprayCan, Building2, Database, Map as MapIcon, Droplets } from "lucide-react";
import LoadingSpinner from "./LoadingSpinner";

const FuelDashboard = lazy(() => import("../app/ops/fuel/page"));
const CustomerTankPage = lazy(() => import("./ops/CustomerTankPage"));
const TruckCompartmentsPage = lazy(() => import("./ops/TruckCompartmentsPage"));
const SourcingPage = lazy(() => import("./ops/SourcingPage"));
const DepotsPage = lazy(() => import("./admin/DepotsPage"));
const RoadRestrictionsPanel = lazy(() => import("./admin/RoadRestrictionsPanel"));
const FuelDistributionPage = lazy(() => import("./ops/FuelDistributionPage"));

const TABS = [
  { id: "stations", label: "Fuel Stations", icon: Fuel },
  { id: "tanks", label: "Customer Tanks", icon: Gauge },
  { id: "compartments", label: "Compartments", icon: SprayCan },
  { id: "sourcing", label: "Sourcing", icon: Building2 },
  { id: "depots", label: "Depots", icon: Database },
  { id: "road-restrictions", label: "Restrictions", icon: MapIcon },
  { id: "distribution", label: "Distribution", icon: Droplets },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function FuelOpsPage() {
  const [activeTab, setActiveTab] = useState<TabId>("stations");

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
        <h1 className="text-xl font-semibold text-gray-900">Fuel Operations</h1>
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
          {activeTab === "stations" && <FuelDashboard />}
          {activeTab === "tanks" && <CustomerTankPage />}
          {activeTab === "compartments" && <TruckCompartmentsPage />}
          {activeTab === "sourcing" && <SourcingPage />}
          {activeTab === "depots" && <DepotsPage />}
          {activeTab === "road-restrictions" && <RoadRestrictionsPanel />}
          {activeTab === "distribution" && <FuelDistributionPage />}
        </Suspense>
      </div>
    </div>
  );
}
