"use client";
import { useState, lazy, Suspense } from "react";
import { Settings, Radio, FileInput, HelpCircle } from "lucide-react";
import LoadingSpinner from "./LoadingSpinner";

const AgentSettingsPage = lazy(() => import("./ops/AgentSettingsPage"));
const OperationsControl = lazy(() => import("../app/ops/control/page"));
const DataImport = lazy(() => import("./DataImport"));
const Support = lazy(() => import("./Support"));

const TABS = [
  { id: "agents", label: "Agent Settings", icon: Settings },
  { id: "control", label: "Control Center", icon: Radio },
  { id: "import", label: "Data Import", icon: FileInput },
  { id: "support", label: "Support", icon: HelpCircle },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<TabId>("agents");

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
        <h1 className="text-xl font-semibold text-gray-900">Settings & Admin</h1>
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
          {activeTab === "agents" && <AgentSettingsPage />}
          {activeTab === "control" && <OperationsControl />}
          {activeTab === "import" && <DataImport />}
          {activeTab === "support" && <Support />}
        </Suspense>
      </div>
    </div>
  );
}
