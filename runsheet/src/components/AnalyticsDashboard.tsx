"use client";

import { lazy, Suspense, useState } from "react";
import { AlertTriangle, BarChart3 } from "lucide-react";
import ErrorBoundary from "./ErrorBoundary";
import LoadingSpinner from "./LoadingSpinner";

const Analytics = lazy(() => import("./Analytics"));
const FailureAnalytics = lazy(() => import("../app/ops/failures/page"));

const TABS = [
  { id: "overview", label: "Overview", icon: BarChart3 },
  { id: "failures", label: "Failure Analytics", icon: AlertTriangle },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function AnalyticsDashboard() {
  const [activeTab, setActiveTab] = useState<TabId>("overview");

  return (
    <div className="flex-1 flex flex-col h-full bg-gray-50">
      {/* Tab bar */}
      <div className="flex items-center gap-1 px-6 pt-4 pb-0">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${
              activeTab === tab.id
                ? "bg-white text-[#232323] border border-gray-200 border-b-white -mb-px z-10"
                : "text-gray-500 hover:text-gray-700 hover:bg-gray-100"
            }`}
          >
            <tab.icon className="w-4 h-4" />
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {activeTab === "overview" && (
          <div className="h-full bg-white rounded-xl shadow-sm border border-gray-200 mx-6 mb-6 mt-4 overflow-hidden">
            <ErrorBoundary componentName="Analytics">
              <Suspense fallback={<LoadingSpinner message="Loading analytics..." />}>
                <Analytics />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}

        {activeTab === "failures" && (
          <div className="h-full bg-white border-t border-gray-200">
            <ErrorBoundary componentName="Failure Analytics">
              <Suspense fallback={<LoadingSpinner message="Loading failure analytics..." />}>
                <FailureAnalytics />
              </Suspense>
            </ErrorBoundary>
          </div>
        )}
      </div>
    </div>
  );
}
