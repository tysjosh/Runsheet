"use client";
import { Activity, AlertTriangle, BarChart3, TrendingUp } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import ErrorBoundary from "./ErrorBoundary";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const Analytics = lazy(() => import("./Analytics"));
const SchedulingMetricsPage = lazy(() => import("./ops/SchedulingMetricsPage"));
const FailureAnalytics = lazy(() => import("../app/ops/failures/page"));
const OpsMonitoringDashboard = lazy(
  () => import("./ops/OpsMonitoringDashboard"),
);

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
    id: "failures",
    label: "Failure Analytics",
    icon: <AlertTriangle className="w-4 h-4" />,
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
          {activeTab === "failures" && (
            <ErrorBoundary componentName="Failure Analytics">
              <FailureAnalytics />
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
