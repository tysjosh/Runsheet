"use client";
import { FileCheck, Gauge, Map, Shield } from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import { canSee, visibleByCanSee } from "../config/modules";
import { getCurrentUserRoles } from "../utils/auth";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const AssetCertificationsPage = lazy(
  () => import("./compliance/AssetCertificationsPage"),
);
const MeterAuditPage = lazy(() => import("./compliance/MeterAuditPage"));
const TerminalBOLsPage = lazy(() => import("./compliance/TerminalBOLsPage"));
const IFTAReportPage = lazy(() => import("./compliance/IFTAReportPage"));

// Exported for the registry drift guard in `config/modules.test.ts`.
export const TABS: Tab[] = [
  {
    id: "certifications",
    label: "Certs",
    icon: <Shield className="w-4 h-4" />,
  },
  { id: "meters", label: "Meters", icon: <Gauge className="w-4 h-4" /> },
  { id: "bols", label: "BOLs", icon: <FileCheck className="w-4 h-4" /> },
  { id: "ifta", label: "IFTA", icon: <Map className="w-4 h-4" /> },
];

type TabId = string;

export default function ComplianceHub() {
  const [activeTab, setActiveTab] = useState<TabId>("certifications");
  const [roles, setRoles] = useState<readonly string[] | null>(null);

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

  const visibleTabs = visibleByCanSee(TABS, { roles });
  const effectiveTab =
    visibleTabs.some((t) => t.id === activeTab) || visibleTabs.length === 0
      ? activeTab
      : visibleTabs[0].id;
  const shows = (id: string) => effectiveTab === id && canSee(id, { roles });

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="Compliance" />
      <TabNavigation
        tabs={visibleTabs}
        activeTab={effectiveTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {shows("certifications") && <AssetCertificationsPage />}
          {shows("meters") && <MeterAuditPage />}
          {shows("bols") && <TerminalBOLsPage />}
          {shows("ifta") && <IFTAReportPage />}
        </Suspense>
      </div>
    </div>
  );
}
