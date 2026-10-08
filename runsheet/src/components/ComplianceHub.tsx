"use client";

import { FileCheck, Gauge, Map, Shield } from "lucide-react";
import { lazy, Suspense } from "react";
import LoadingSpinner from "./LoadingSpinner";
import { useHubTabs } from "./shell/useHubTabs";
import { PageChromeProvider, PageHeader, type Tab, TabPanel } from "./ui";

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
    label: "Certifications",
    icon: <Shield className="w-4 h-4" />,
  },
  { id: "meters", label: "Meters", icon: <Gauge className="w-4 h-4" /> },
  { id: "bols", label: "BOLs", icon: <FileCheck className="w-4 h-4" /> },
  { id: "ifta", label: "IFTA", icon: <Map className="w-4 h-4" /> },
];

export default function ComplianceHub() {
  const { tabs, active, setActive, shows } = useHubTabs(TABS);
  return (
    <PageChromeProvider>
      <div className="flex flex-col h-full">
        <PageHeader
          host
          title="Compliance"
          tabs={tabs}
          tab={active}
          onTabChange={setActive}
          tabIdBase="compliance"
        />
        <TabPanel
          idBase="compliance"
          value={active}
          className="flex-1 overflow-auto"
        >
          <Suspense fallback={<LoadingSpinner message="Loading..." />}>
            {shows("certifications") && <AssetCertificationsPage />}
            {shows("meters") && <MeterAuditPage />}
            {shows("bols") && <TerminalBOLsPage />}
            {shows("ifta") && <IFTAReportPage />}
          </Suspense>
        </TabPanel>
      </div>
    </PageChromeProvider>
  );
}
