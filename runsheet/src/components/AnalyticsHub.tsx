"use client";

import { Activity, BarChart3, Gauge, TrendingUp } from "lucide-react";
import { lazy, Suspense } from "react";
import ErrorBoundary from "./ErrorBoundary";
import LoadingSpinner from "./LoadingSpinner";
import { useHubTabs } from "./shell/useHubTabs";
import { PageChromeProvider, PageHeader, type Tab, TabPanel } from "./ui";

const Analytics = lazy(() => import("./Analytics"));
const SchedulingMetricsPage = lazy(() => import("./ops/SchedulingMetricsPage"));
// NOTE: a "Failure Analytics" tab used to live here, rendering
// `app/ops/failures/page`. It read `/ops/metrics/failures` and
// `/ops/shipments/failures`, both of which sit behind
// `require_ops_enabled` -> `LEGACY_NG_DELIVERY_DISABLED`. With that flag off
// everywhere the tab could only ever throw, so the tab and the page were
// removed rather than left as a guaranteed error surface.
const OpsMonitoringDashboard = lazy(
  () => import("./ops/OpsMonitoringDashboard"),
);
const FuelEfficiencyChart = lazy(() => import("./ops/FuelEfficiencyChart"));

const TABS: Tab[] = [
  {
    id: "overview",
    label: "Overview",
    icon: <BarChart3 className="w-4 h-4" />,
  },
  {
    id: "scheduling",
    label: "Scheduling metrics",
    icon: <TrendingUp className="w-4 h-4" />,
  },
  {
    id: "fleet-efficiency",
    label: "Fleet efficiency",
    icon: <Gauge className="w-4 h-4" />,
  },
  // The poison queue is platform-wide and `/ops/monitoring/poison-queue` is
  // `platform_admin` only, so tenant users don't get the tab at all. (Task 3.7
  // moves it to Settings → System health.)
  {
    id: "ops-monitoring",
    label: "Ops monitoring",
    icon: <Activity className="w-4 h-4" />,
  },
];

export default function AnalyticsHub() {
  const { tabs, active, setActive, shows } = useHubTabs(TABS, {
    visible: (tab, roles) =>
      tab.id !== "ops-monitoring" || Boolean(roles?.includes("platform_admin")),
  });
  return (
    <PageChromeProvider>
      <div className="flex flex-col h-full">
        <PageHeader
          host
          title="Analytics"
          tabs={tabs}
          tab={active}
          onTabChange={setActive}
          tabIdBase="analytics"
        />
        <TabPanel
          idBase="analytics"
          value={active}
          className="flex-1 overflow-auto"
        >
          <Suspense fallback={<LoadingSpinner message="Loading..." />}>
            {shows("overview") && (
              <ErrorBoundary componentName="Analytics">
                <Analytics />
              </ErrorBoundary>
            )}
            {shows("scheduling") && (
              <ErrorBoundary componentName="Scheduling Metrics">
                <SchedulingMetricsPage />
              </ErrorBoundary>
            )}
            {shows("fleet-efficiency") && (
              <ErrorBoundary componentName="Fleet Efficiency">
                <div className="p-6">
                  <div className="mb-4">
                    <h2 className="text-base font-semibold text-primary">
                      Fleet Fuel Efficiency
                    </h2>
                    <p className="text-sm text-gray-500 mt-0.5">
                      Per-vehicle fuel economy (km/L) derived from consumption
                      events and odometer readings. Higher is better.
                    </p>
                  </div>
                  <FuelEfficiencyChart />
                </div>
              </ErrorBoundary>
            )}
            {shows("ops-monitoring") && (
              <ErrorBoundary componentName="Ops Monitoring">
                <OpsMonitoringDashboard />
              </ErrorBoundary>
            )}
          </Suspense>
        </TabPanel>
      </div>
    </PageChromeProvider>
  );
}
