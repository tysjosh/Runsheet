"use client";

import { FileInput, HelpCircle, Settings } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const AgentSettingsPage = lazy(() => import("./ops/AgentSettingsPage"));
const DataImport = lazy(() => import("./DataImport"));
const Support = lazy(() => import("./Support"));

const TABS: Tab[] = [
  {
    id: "agents",
    label: "Agent Settings",
    icon: <Settings className="w-4 h-4" />,
  },
  {
    id: "import",
    label: "Data Import",
    icon: <FileInput className="w-4 h-4" />,
  },
  {
    id: "support",
    label: "Support",
    icon: <HelpCircle className="w-4 h-4" />,
  },
];

type TabId = string;

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<TabId>("agents");

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="Settings & Admin" />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {activeTab === "agents" && <AgentSettingsPage />}
          {activeTab === "import" && <DataImport />}
          {activeTab === "support" && <Support />}
        </Suspense>
      </div>
    </div>
  );
}
