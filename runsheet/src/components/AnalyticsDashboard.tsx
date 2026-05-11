"use client";

import { AlertTriangle, BarChart3, TrendingUp } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import ErrorBoundary from "./ErrorBoundary";
import LoadingSpinner from "./LoadingSpinner";
import { type Tab, TabNavigation } from "./ui";

const Analytics = lazy(() => import("./Analytics"));
const FailureAnalytics = lazy(() => import("../app/ops/failures/page"));
const SchedulingMetricsPage = lazy(() => import("./ops/SchedulingMetricsPage"));

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
];

type TabId = string;

export default function AnalyticsDashboard() {
  const [activeTab, setActiveTab] = useState<TabId>("overview");

  return (
    <div className="flex-1 flex flex-col h-full bg-gray-50">
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
        className="pt-4 bg-gray-50"
      />

      {/* Tab content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {activeTab === "overview" && (
          <div className="h-full bg-white rounded-xl shadow-sm border border-gray-200 mx-6 mb-6 mt-4 overflow-hidden">
            <ErrorBoundary componentName="Analytics">
              <Suspense
                fallback={<LoadingSpinner message="Loading analytics..." />}
              >
                <Analytics />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}

        {activeTab === "failures" && (
          <div className="h-full bg-white border-t border-gray-200">
            <ErrorBoundary componentName="Failure Analytics">
              <Suspense
                fallback={
                  <LoadingSpinner message="Loading failure analytics..." />
                }
              >
                <FailureAnalytics />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}

        {activeTab === "scheduling" && (
          <div className="h-full bg-white border-t border-gray-200 overflow-auto">
            <ErrorBoundary componentName="Scheduling Metrics">
              <Suspense
                fallback={
                  <LoadingSpinner message="Loading scheduling metrics..." />
                }
              >
                <SchedulingMetricsPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}
      </div>
    </div>
  );
}
