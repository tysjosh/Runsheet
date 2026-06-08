"use client";
import { FileCheck, Gauge, Map, Shield } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const AssetCertificationsPage = lazy(
  () => import("./compliance/AssetCertificationsPage"),
);
const MeterAuditPage = lazy(() => import("./compliance/MeterAuditPage"));
const TerminalBOLsPage = lazy(() => import("./compliance/TerminalBOLsPage"));
const IFTAReportPage = lazy(() => import("./compliance/IFTAReportPage"));

const TABS: Tab[] = [
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

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="Compliance" />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {activeTab === "certifications" && <AssetCertificationsPage />}
          {activeTab === "meters" && <MeterAuditPage />}
          {activeTab === "bols" && <TerminalBOLsPage />}
          {activeTab === "ifta" && <IFTAReportPage />}
        </Suspense>
      </div>
    </div>
  );
}
