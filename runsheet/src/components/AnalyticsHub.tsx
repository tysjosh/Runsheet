"use client";

import { BarChart3, Gauge, TrendingUp } from "lucide-react";
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
  // Ops monitoring is retired from Analytics (D12): its useful remainder,
  // poison-queue depth, is Settings → System health for platform_admin.
];

const ALWAYS = () => true;

export default function AnalyticsHub() {
  // The hub is gated by the `analytics` module; its tabs have no gates of
  // their own.
  const { tabs, active, setActive, shows } = useHubTabs(TABS, {
    visible: ALWAYS,
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
                <div className="p-4">
                  <div className="mb-3">
                    <h2 className="text-sm font-semibold text-text">
                      Fleet fuel efficiency
                    </h2>
                    <p className="mt-0.5 text-xs text-text-muted">
                      Per-vehicle fuel economy (km/L) from consumption events
                      and odometer readings. Higher is better.
                    </p>
                  </div>
                  <FuelEfficiencyChart />
                </div>
              </ErrorBoundary>
            )}
          </Suspense>
        </TabPanel>
      </div>
    </PageChromeProvider>
  );
}
