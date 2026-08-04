"use client";

import { lazy, Suspense, useEffect, useState } from "react";
import { canSee, visibleByCanSee } from "../config/modules";
import { getCurrentUserRoles } from "../utils/auth";
import { type Tab, TabNavigation } from "./ui";

// Lazy load admin components
const NotificationMetricsDashboard = lazy(
  () => import("./admin/NotificationMetricsDashboard"),
);
const FeatureFlagsAdmin = lazy(() => import("./admin/FeatureFlagsAdmin"));
const AgentMonitoringDashboard = lazy(
  () => import("./admin/AgentMonitoringDashboard"),
);
const StripeIntegrationUI = lazy(() => import("./admin/StripeIntegrationUI"));
const IntegrationMarketplacePage = lazy(
  () => import("../app/admin/integrations/page"),
);
const IntakeChannelsAdminPanel = lazy(
  () => import("./admin/IntakeChannelsAdminPanel"),
);
const WeatherAlertsPage = lazy(
  () => import("../app/admin/weather-alerts/page"),
);
const DataImport = lazy(() => import("./DataImport"));
// Agent policy — autonomy level, pause/resume, memory. Moved here from the
// former top-level Settings nav item, which had emptied down to this one tab.
// It sits beside `agents` (Agent Monitoring): monitoring is what the agents did,
// this is what they are allowed to do.
const AgentSettingsPage = lazy(() => import("./ops/AgentSettingsPage"));

function LoadingPlaceholder() {
  return (
    <div className="flex justify-center items-center py-12">
      <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
    </div>
  );
}

// Hoisted out of the component and exported so the registry drift guard in
// `config/modules.test.ts` reads the real array rather than a copy of it.
export const TABS: Tab[] = [
  {
    id: "metrics",
    label: "Notification Metrics",
  },
  {
    id: "feature-flags",
    label: "Feature Flags",
  },
  {
    id: "agents",
    label: "Agent Monitoring",
  },
  {
    id: "stripe",
    label: "Stripe Integration",
  },
  {
    id: "integrations",
    label: "Integrations",
  },
  {
    id: "intake-channels",
    label: "Intake Channels",
  },
  {
    id: "weather-alerts",
    label: "Weather Alerts",
  },
  // Moved here from the Settings hub. It is admin-only
  // (`import_endpoints.py::IMPORT_ADMIN_ROLES`), and the `admin` nav item that
  // leads to this hub requires the same role — so the gate and its container now
  // agree. Under Settings the tab was the only gated thing in an ungated hub,
  // which meant a dispatcher saw a nav item leading to a tab bar missing its
  // main entry.
  {
    id: "import",
    label: "Data Import",
  },
  {
    id: "agent-settings",
    label: "Agent Settings",
  },
];

export default function AdminHub({
  initialTab = "metrics",
}: {
  /** Tab to open on mount (e.g. deep-linked from the Storm_Mode banner). */
  initialTab?: string;
} = {}) {
  const [activeTab, setActiveTab] = useState(initialTab);
  // `null` until the session resolves; `canSee` treats that as no roles, so the
  // admin-only Feature Flags tab never flashes visible.
  const [roles, setRoles] = useState<readonly string[] | null>(null);

  // Honor a changed deep-link target (e.g. banner → Weather Alerts) even when
  // AdminHub is already mounted in the shell.
  useEffect(() => {
    setActiveTab(initialTab);
  }, [initialTab]);

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

  const tabs = visibleByCanSee(TABS, { roles });
  // A `?tab=` deep link can name a tab this user cannot see. Fall back to the
  // first visible tab rather than rendering a blank pane.
  const effectiveTab =
    tabs.some((t) => t.id === activeTab) || tabs.length === 0
      ? activeTab
      : tabs[0].id;

  const renderContent = () => {
    // Belt and braces: the tab bar already omits hidden tabs, but a deep link
    // sets `activeTab` directly, so re-check before rendering the panel.
    if (!canSee(effectiveTab, { roles })) return null;
    switch (effectiveTab) {
      case "metrics":
        return (
          <Suspense fallback={<LoadingPlaceholder />}>
            <NotificationMetricsDashboard />
          </Suspense>
        );
      case "feature-flags":
        return (
          <Suspense fallback={<LoadingPlaceholder />}>
            <FeatureFlagsAdmin />
          </Suspense>
        );
      case "webhooks":
        return (
          <Suspense fallback={<LoadingPlaceholder />}>
            <IntakeChannelsAdminPanel />
          </Suspense>
        );
      case "agents":
        return (
          <Suspense fallback={<LoadingPlaceholder />}>
            <AgentMonitoringDashboard />
          </Suspense>
        );
      case "stripe":
        return (
          <Suspense fallback={<LoadingPlaceholder />}>
            <StripeIntegrationUI />
          </Suspense>
        );
      case "integrations":
        return (
          <Suspense fallback={<LoadingPlaceholder />}>
            <IntegrationMarketplacePage />
          </Suspense>
        );
      case "intake-channels":
        return (
          <Suspense fallback={<LoadingPlaceholder />}>
            <IntakeChannelsAdminPanel />
          </Suspense>
        );
      case "weather-alerts":
        return (
          <Suspense fallback={<LoadingPlaceholder />}>
            <WeatherAlertsPage />
          </Suspense>
        );
      case "import":
        return (
          <Suspense fallback={<LoadingPlaceholder />}>
            <DataImport />
          </Suspense>
        );
      case "agent-settings":
        return (
          <Suspense fallback={<LoadingPlaceholder />}>
            <AgentSettingsPage />
          </Suspense>
        );
      default:
        return null;
    }
  };

  return (
    <div className="h-full flex flex-col bg-gray-50">
      <div className="bg-white border-b border-gray-200 px-6 pt-6">
        <div className="mb-6">
          <h1 className="text-2xl font-bold text-gray-900">
            System Administration
          </h1>
          <p className="text-gray-600 mt-1">
            Monitor system health and manage platform configuration
          </p>
        </div>
        <TabNavigation
          tabs={tabs}
          activeTab={effectiveTab}
          onChange={setActiveTab}
        />
      </div>
      <div className="flex-1 overflow-auto">{renderContent()}</div>
    </div>
  );
}
