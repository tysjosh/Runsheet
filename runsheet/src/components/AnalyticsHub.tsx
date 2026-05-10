"use client";
import { useState, lazy, Suspense } from "react";
import { BarChart3, TrendingUp, AlertTriangle, Activity } from "lucide-react";
import ErrorBoundary from "./ErrorBoundary";
import LoadingSpinner from "./LoadingSpinner";

const Analytics = lazy(() => import("./Analytics"));
const SchedulingMetricsPage = lazy(() => import("./ops/SchedulingMetricsPage"));
const FailureAnalytics = lazy(() => import("../app/ops/failures/page"));
const OpsMonitoringDashboard = lazy(() => import("./ops/OpsMonitoringDashboard"));

const TABS = [
  { id: "overview", label: "Overview", icon: BarChart3 },
  { id: "scheduling", label: "Scheduling Metrics", icon: TrendingUp },
  { id: "failures", label: "Failure Analytics", icon: AlertTriangle },
  { id: "ops-monitoring", label: "Ops Monitoring", icon: Activity },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function AnalyticsHub() {
  const [activeTab, setActiveTab] = useState<TabId>("overview");

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
        <h1 className="text-xl font-semibold text-gray-900">Analytics & Monitoring</h1>
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
