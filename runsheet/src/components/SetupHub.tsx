"use client";

/**
 * SetupHub — one home for occasional configuration / reference data that
 * previously cluttered the operational hubs as co-equal tabs.
 *
 * Consolidates:
 *  • Depots and Road Restrictions (formerly Fuel Ops tabs)
 *  • Tax Jurisdictions, Exemptions, and K-Factor calibration (formerly
 *    Compliance tabs)
 *
 * These are setup tasks a dispatcher touches rarely, so they live apart from
 * the daily-work surfaces rather than beside them.
 */

import { Database, Map as MapIcon, Receipt, ScrollText } from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import { getCurrentUserRoles } from "../utils/auth";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const DepotsPage = lazy(() => import("./admin/DepotsPage"));
const RoadRestrictionsPanel = lazy(
  () => import("./admin/RoadRestrictionsPanel"),
);
const TaxJurisdictionsPage = lazy(
  () => import("./compliance/TaxJurisdictionsPage"),
);
const ExemptionsPage = lazy(() => import("./compliance/ExemptionsPage"));

const TABS: Tab[] = [
  { id: "depots", label: "Depots", icon: <Database className="w-4 h-4" /> },
  {
    id: "road-restrictions",
    label: "Road Restrictions",
    icon: <MapIcon className="w-4 h-4" />,
  },
  {
    id: "tax",
    label: "Tax Jurisdictions",
    icon: <Receipt className="w-4 h-4" />,
  },
  {
    id: "exemptions",
    label: "Exemptions",
    icon: <ScrollText className="w-4 h-4" />,
  },
];

type TabId = string;

export default function SetupHub() {
  const [activeTab, setActiveTab] = useState<TabId>("depots");
  // Session roles gate the Storm_Mode road-restriction upload form (the
  // backend re-checks on submit).
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
      <PageHeader
        title="Setup"
        subtitle="Configuration and reference data"
        icon={<Database className="w-5 h-5" />}
      />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {activeTab === "depots" && <DepotsPage />}
          {activeTab === "road-restrictions" && (
            <RoadRestrictionsPanel roles={roles} />
          )}
          {activeTab === "tax" && <TaxJurisdictionsPage />}
          {activeTab === "exemptions" && <ExemptionsPage />}
        </Suspense>
      </div>
    </div>
  );
}
