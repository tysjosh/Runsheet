"use client";

import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { lazy, Suspense, useEffect, useState } from "react";
import ErrorBoundary from "../../components/ErrorBoundary";
import Header from "../../components/Header";
import LoadingSpinner from "../../components/LoadingSpinner";
import Sidebar from "../../components/Sidebar";
import type { Truck } from "../../types/api";

// MapView — heavy Google Maps library, no SSR
const MapView = dynamic(() => import("../../components/MapView"), {
  loading: () => <MapLoadingPlaceholder />,
  ssr: false,
});

// Lazy-load all content components
const FleetDashboard = lazy(() => import("../../components/FleetDashboard"));
const AIChat = lazy(() => import("../../components/AIChat"));
const DataUpload = lazy(() => import("../../components/DataUpload"));
const Inventory = lazy(() => import("../../components/Inventory"));
const AnalyticsDashboard = lazy(() => import("../../components/AnalyticsDashboard"));
const Support = lazy(() => import("../../components/Support"));

// Ops components (previously on /ops/* routes)
const SchedulingJobBoard = lazy(() => import("../ops/scheduling/page"));
const FuelDashboard = lazy(() => import("../ops/fuel/page"));
const RiderUtilization = lazy(() => import("../ops/riders/page"));
const OperationsControl = lazy(() => import("../ops/control/page"));

// New feature pages
const FuelDistributionPage = lazy(() => import("../../components/ops/FuelDistributionPage"));
const AgentSettingsPage = lazy(() => import("../../components/ops/AgentSettingsPage"));
const OpsMonitoringDashboard = lazy(() => import("../../components/ops/OpsMonitoringDashboard"));
const SchedulingMetricsPage = lazy(() => import("../../components/ops/SchedulingMetricsPage"));

function MapLoadingPlaceholder() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <div className="text-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gray-900 mx-auto"></div>
        <p className="mt-2 text-gray-600">Loading map...</p>
      </div>
    </div>
  );
}

function ComponentLoadingPlaceholder() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function Home() {
  const router = useRouter();
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true);
  const [activeMenuItem, setActiveMenuItem] = useState(() => {
    if (typeof window !== "undefined") {
      return sessionStorage.getItem("activeMenuItem") || "fleet";
    }
    return "fleet";
  });
  const [selectedTruck, setSelectedTruck] = useState<Truck | null>(null);
  const [aiChatOpen, setAiChatOpen] = useState(false);

  // Persist active menu item across refreshes
  const handleNavigate = (item: string) => {
    setActiveMenuItem(item);
    if (typeof window !== "undefined") {
      sessionStorage.setItem("activeMenuItem", item);
    }
  };

  useEffect(() => {
    if (typeof window !== "undefined") {
      const authStatus = sessionStorage.getItem("isAuthenticated");
      if (authStatus === "true") {
        setIsAuthenticated(true);
      } else {
        sessionStorage.removeItem("isAuthenticated");
        router.replace("/signin");
      }
      setIsLoading(false);
    }
  }, [router]);

  const handleTruckSelect = (truck: Truck) => {
    setSelectedTruck(truck);
  };

  const renderMainContent = () => {
    switch (activeMenuItem) {
      case "upload-data":
        return (
          <div className="flex-1 p-6 bg-gray-50">
            <div className="bg-white rounded-xl shadow-sm border border-gray-200 h-full overflow-hidden">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <DataUpload />
              </Suspense>
            </div>
          </div>
        );

      case "fleet":
        return (
          <Suspense fallback={<ComponentLoadingPlaceholder />}>
            <FleetDashboard
              selectedTruck={selectedTruck}
              onTruckSelect={handleTruckSelect}
              mapView={<MapView selectedTruck={selectedTruck} />}
            />
          </Suspense>
        );

      case "scheduling":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Scheduling">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <SchedulingJobBoard />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "inventory":
        return (
          <div className="flex-1 p-6 bg-gray-50">
            <div className="bg-white rounded-xl shadow-sm border border-gray-200 h-full overflow-hidden">
              <ErrorBoundary componentName="Inventory">
                <Suspense fallback={<ComponentLoadingPlaceholder />}>
                  <Inventory />
                </Suspense>
              </ErrorBoundary>
            </div>
          </div>
        );

      case "fuel":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Fuel">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <FuelDashboard />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "riders":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Riders">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <RiderUtilization />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "analytics":
        return (
          <Suspense fallback={<ComponentLoadingPlaceholder />}>
            <AnalyticsDashboard />
          </Suspense>
        );

      case "control-center":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Control Center">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <OperationsControl />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "fuel-distribution":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Fuel Distribution">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <FuelDistributionPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "agent-settings":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Agent Settings">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <AgentSettingsPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "ops-monitoring":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Ops Monitoring">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <OpsMonitoringDashboard />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "scheduling-metrics":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Scheduling Metrics">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <SchedulingMetricsPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "support":
        return (
          <div className="flex-1 p-6 bg-gray-50">
            <div className="bg-white rounded-xl shadow-sm border border-gray-200 h-full overflow-hidden">
              <ErrorBoundary componentName="Support">
                <Suspense fallback={<ComponentLoadingPlaceholder />}>
                  <Support />
                </Suspense>
              </ErrorBoundary>
            </div>
          </div>
        );

      default:
        return (
          <div className="flex-1 flex items-center justify-center bg-white">
            <div className="text-center">
              <h2 className="text-xl font-semibold text-gray-700 mb-2">
                Welcome to RUNSHEET
              </h2>
              <p className="text-gray-500">
                Select a module from the sidebar to get started
              </p>
            </div>
          </div>
        );
    }
  };

  if (isLoading) {
    return (
      <div className="h-screen flex items-center justify-center bg-gray-50">
        <div className="text-center">
          <div className="w-8 h-8 border-4 border-gray-300 border-t-blue-600 rounded-full animate-spin mx-auto mb-4"></div>
          <p className="text-gray-600">Loading...</p>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) return null;

  return (
    <div className="h-screen flex flex-col bg-white">
      <div className="flex flex-1 overflow-hidden">
        <Sidebar
          activeItem={activeMenuItem}
          isCollapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
          onNavigate={handleNavigate}
        />
        <div
          className="flex-1 flex flex-col min-h-0 overflow-hidden"
          style={{ minWidth: 0 }}
        >
          <Header onAIClick={() => setAiChatOpen(true)} />
          <main className="flex-1 flex bg-white relative z-0 overflow-hidden">
            <div className="flex-1 flex bg-white overflow-auto">
              {renderMainContent()}
            </div>
          </main>
        </div>
      </div>
      <ErrorBoundary componentName="AI Chat">
        <Suspense fallback={null}>
          <AIChat isOpen={aiChatOpen} onClose={() => setAiChatOpen(false)} />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
