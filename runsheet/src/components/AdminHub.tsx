"use client";

import { lazy, Suspense, useState } from "react";
import { TabNavigation, type Tab } from "./ui";

// Lazy load admin components
const NotificationMetricsDashboard = lazy(
  () => import("./admin/NotificationMetricsDashboard"),
);
const FeatureFlagsAdmin = lazy(() => import("./admin/FeatureFlagsAdmin"));
const OrderWebhooksAdmin = lazy(() => import("./admin/OrderWebhooksAdmin"));
const AgentMonitoringDashboard = lazy(
  () => import("./admin/AgentMonitoringDashboard"),
);
const StripeIntegrationUI = lazy(() => import("./admin/StripeIntegrationUI"));

function LoadingPlaceholder() {
  return (
    <div className="flex justify-center items-center py-12">
      <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
    </div>
  );
}

export default function AdminHub() {
  const [activeTab, setActiveTab] = useState("metrics");

  const tabs: Tab[] = [
    {
      id: "metrics",
      label: "Notification Metrics",
    },
    {
      id: "feature-flags",
      label: "Feature Flags",
    },
    {
      id: "webhooks",
      label: "Order Webhooks",
    },
    {
      id: "agents",
      label: "Agent Monitoring",
    },
    {
      id: "stripe",
      label: "Stripe Integration",
    },
  ];

  const renderContent = () => {
    switch (activeTab) {
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
            <OrderWebhooksAdmin />
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
        <TabNavigation tabs={tabs} activeTab={activeTab} onChange={setActiveTab} />
      </div>
      <div className="flex-1 overflow-auto">{renderContent()}</div>
    </div>
  );
}
