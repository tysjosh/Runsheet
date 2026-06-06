"use client";
import {
  Building2,
  Database,
  Fuel,
  Gauge,
  Map as MapIcon,
  SprayCan,
} from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import { getCurrentUserRoles } from "../utils/auth";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const FuelDashboard = lazy(() => import("../app/ops/fuel/page"));
const CustomerTankPage = lazy(() => import("./ops/CustomerTankPage"));
const TruckCompartmentsPage = lazy(() => import("./ops/TruckCompartmentsPage"));
const SourcingPage = lazy(() => import("./ops/SourcingPage"));
const DepotsPage = lazy(() => import("./admin/DepotsPage"));
const RoadRestrictionsPanel = lazy(
  () => import("./admin/RoadRestrictionsPanel"),
);

const TABS: Tab[] = [
  {
    id: "stations",
    label: "Fuel Stations",
    icon: <Fuel className="w-4 h-4" />,
  },
  { id: "tanks", label: "Customer Tanks", icon: <Gauge className="w-4 h-4" /> },
  {
    id: "compartments",
    label: "Compartments",
    icon: <SprayCan className="w-4 h-4" />,
  },
  {
    id: "sourcing",
    label: "Sourcing",
    icon: <Building2 className="w-4 h-4" />,
  },
  { id: "depots", label: "Depots", icon: <Database className="w-4 h-4" /> },
  {
    id: "road-restrictions",
    label: "Restrictions",
    icon: <MapIcon className="w-4 h-4" />,
  },
];

type TabId = string;

export default function FuelOpsPage() {
  const [activeTab, setActiveTab] = useState<TabId>("stations");
  // Session roles gate the Storm_Mode road-restriction upload form (the
  // backend re-checks on submit). Without this, the panel was mounted with no
  // roles and the upload form was permanently hidden.
  const [roles, setRoles] = useState<readonly string[]>([]);

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

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="Fuel Operations" />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {activeTab === "stations" && <FuelDashboard />}
          {activeTab === "tanks" && <CustomerTankPage />}
          {activeTab === "compartments" && <TruckCompartmentsPage />}
          {activeTab === "sourcing" && <SourcingPage />}
          {activeTab === "depots" && <DepotsPage />}
          {activeTab === "road-restrictions" && (
            <RoadRestrictionsPanel roles={roles} />
          )}
        </Suspense>
      </div>
    </div>
  );
}
