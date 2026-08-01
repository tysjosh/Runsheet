"use client";

import { FileInput, HelpCircle, Lock, Settings } from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import { canSee, visibleByCanSee } from "../config/modules";
import { getCurrentUserRoles } from "../utils/auth";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const AgentSettingsPage = lazy(() => import("./ops/AgentSettingsPage"));
const DataImport = lazy(() => import("./DataImport"));
const Support = lazy(() => import("./Support"));
const ChangePassword = lazy(() => import("./ChangePassword"));

/**
 * The Agent Settings tab is `agent-settings`, not `agents`: AdminHub already
 * owns `agents` (Agent Monitoring), and module ids share one flat namespace so
 * `canSee` stays a one-argument question. Nothing deep-links this hub's tabs, so
 * the rename is internal.
 */
export const TABS: Tab[] = [
  {
    id: "agent-settings",
    label: "Agent Settings",
    icon: <Settings className="w-4 h-4" />,
  },
  {
    id: "import",
    label: "Data Import",
    icon: <FileInput className="w-4 h-4" />,
  },
  {
    id: "security",
    label: "Security",
    icon: <Lock className="w-4 h-4" />,
  },
  {
    id: "support",
    label: "Support",
    icon: <HelpCircle className="w-4 h-4" />,
  },
];

type TabId = string;

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<TabId>("agent-settings");
  // `null` until the session resolves. Data Import is admin/dispatcher only
  // (import_endpoints.py::_require_import_role), and the tab must not flash
  // visible before we know. The Security tab is never role-gated — it holds
  // password change, which every user needs.
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
  // Keep the selection on a tab the user can actually see — a hidden tab left
  // selected renders an empty pane under a tab bar that no longer offers it.
  const effectiveTab =
    visibleTabs.some((t) => t.id === activeTab) || visibleTabs.length === 0
      ? activeTab
      : visibleTabs[0].id;
  const shows = (id: string) => effectiveTab === id && canSee(id, { roles });

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="Settings & Admin" />
      <TabNavigation
        tabs={visibleTabs}
        activeTab={effectiveTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {shows("agent-settings") && <AgentSettingsPage />}
          {shows("import") && <DataImport />}
          {shows("security") && <ChangePassword />}
          {shows("support") && <Support />}
        </Suspense>
      </div>
    </div>
  );
}
