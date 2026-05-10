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

// Lazy-load content components
const FleetDashboard = lazy(() => import("../../components/FleetDashboard"));
const AIChat = lazy(() => import("../../components/AIChat"));
const CommerceBillingPage = lazy(() => import("../commerce/page"));

// New grouped hub pages
const DispatchPage = lazy(() => import("../../components/DispatchPage"));
const FuelOpsPage = lazy(() => import("../../components/FuelOpsPage"));
const ReconciliationHub = lazy(() => import("../../components/ReconciliationHub"));
const AnalyticsHub = lazy(() => import("../../components/AnalyticsHub"));
const SettingsPage = lazy(() => import("../../components/SettingsPage"));
const CustomersPage = lazy(() => import("../../components/commerce/CustomersListPage"));
const DriversPage = lazy(() => import("../ops/drivers/page"));

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

      case "dispatch":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Dispatch">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <DispatchPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "customers":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Customers">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <CustomersPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "drivers":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Drivers">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <DriversPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "fuel-ops":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Fuel Ops">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <FuelOpsPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "billing":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Billing">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <CommerceBillingPage />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "reconciliation":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Reconciliation">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <ReconciliationHub />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "analytics":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Analytics">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <AnalyticsHub />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "settings":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Settings">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <SettingsPage />
              </Suspense>
            </ErrorBoundary>
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
