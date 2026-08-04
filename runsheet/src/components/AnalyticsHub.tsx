"use client";
import { Activity, BarChart3, Gauge, TrendingUp } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import ErrorBoundary from "./ErrorBoundary";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

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
    label: "Scheduling Metrics",
    icon: <TrendingUp className="w-4 h-4" />,
  },
  {
    id: "fleet-efficiency",
    label: "Fleet Efficiency",
    icon: <Gauge className="w-4 h-4" />,
  },
  {
    id: "ops-monitoring",
    label: "Ops Monitoring",
    icon: <Activity className="w-4 h-4" />,
  },
];

type TabId = string;

export default function AnalyticsHub() {
  const [activeTab, setActiveTab] = useState<TabId>("overview");

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="Analytics & Monitoring" />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {activeTab === "overview" && (
            <ErrorBoundary componentName="Analytics">
              <Analytics />
            </ErrorBoundary>
          )}
          {activeTab === "scheduling" && (
            <ErrorBoundary componentName="Scheduling Metrics">
              <SchedulingMetricsPage />
            </ErrorBoundary>
          )}
          {activeTab === "fleet-efficiency" && (
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
          {activeTab === "ops-monitoring" && (
            <ErrorBoundary componentName="Ops Monitoring">
              <OpsMonitoringDashboard />
            </ErrorBoundary>
          )}
        </Suspense>
      </div>
    </div>
  );
}
