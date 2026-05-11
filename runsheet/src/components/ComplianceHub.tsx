"use client";
import { useState, lazy, Suspense } from "react";
import {
  User,
  Shield,
  Receipt,
  FileText,
  DollarSign,
  Gauge,
  Activity,
  FileCheck,
  Map,
} from "lucide-react";
import LoadingSpinner from "./LoadingSpinner";

const DriversPage = lazy(() => import("./compliance/DriversPage"));
const AssetCertificationsPage = lazy(() => import("./compliance/AssetCertificationsPage"));
const TaxJurisdictionsPage = lazy(() => import("./compliance/TaxJurisdictionsPage"));
const PriceProtectionContractsPage = lazy(() => import("./compliance/PriceProtectionContractsPage"));
const PricingRulesPage = lazy(() => import("./compliance/PricingRulesPage"));
const MeterAuditPage = lazy(() => import("./compliance/MeterAuditPage"));
const KFactorCalibrationPage = lazy(() => import("./compliance/KFactorCalibrationPage"));
const TerminalBOLsPage = lazy(() => import("./compliance/TerminalBOLsPage"));
const IFTAReportPage = lazy(() => import("./compliance/IFTAReportPage"));

const TABS = [
  { id: "drivers", label: "Drivers", icon: User },
  { id: "certifications", label: "Certs", icon: Shield },
  { id: "tax", label: "Tax", icon: Receipt },
  { id: "contracts", label: "Contracts", icon: FileText },
  { id: "pricing", label: "Pricing", icon: DollarSign },
  { id: "meters", label: "Meters", icon: Gauge },
  { id: "kfactor", label: "K-Factor", icon: Activity },
  { id: "bols", label: "BOLs", icon: FileCheck },
  { id: "ifta", label: "IFTA", icon: Map },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function ComplianceHub() {
  const [activeTab, setActiveTab] = useState<TabId>("drivers");

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
        <h1 className="text-xl font-semibold text-gray-900">Compliance</h1>
      </div>
      <div className="flex border-b border-gray-200 px-6 overflow-x-auto">
        {TABS.map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap ${
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
          {activeTab === "drivers" && <DriversPage />}
          {activeTab === "certifications" && <AssetCertificationsPage />}
          {activeTab === "tax" && <TaxJurisdictionsPage />}
          {activeTab === "contracts" && <PriceProtectionContractsPage />}
          {activeTab === "pricing" && <PricingRulesPage />}
          {activeTab === "meters" && <MeterAuditPage />}
          {activeTab === "kfactor" && <KFactorCalibrationPage />}
          {activeTab === "bols" && <TerminalBOLsPage />}
          {activeTab === "ifta" && <IFTAReportPage />}
        </Suspense>
      </div>
    </div>
  );
}
