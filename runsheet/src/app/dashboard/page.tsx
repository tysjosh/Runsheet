"use client";

import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { lazy, Suspense, useEffect, useState } from "react";
import Session from "supertokens-auth-react/recipe/session";
import ErrorBoundary from "../../components/ErrorBoundary";
import Header from "../../components/Header";
import LoadingSpinner from "../../components/LoadingSpinner";
import Sidebar from "../../components/Sidebar";
import { InShellNavProvider } from "../../components/ui/InShellNav";
import type { Truck } from "../../types/api";

// MapView — heavy Google Maps library, no SSR
const MapView = dynamic(() => import("../../components/MapView"), {
  loading: () => <MapLoadingPlaceholder />,
  ssr: false,
});

// Lazy-load content components
const FleetDashboard = lazy(() => import("../../components/FleetDashboard"));
const AIChat = lazy(() => import("../../components/AIChat"));

// New grouped hub pages
const DispatchPage = lazy(() => import("../../components/DispatchPage"));
const FuelOpsPage = lazy(() => import("../../components/FuelOpsPage"));
const ComplianceHub = lazy(() => import("../../components/ComplianceHub"));
const CommerceHub = lazy(() => import("../../components/CommerceHub"));
const AnalyticsHub = lazy(() => import("../../components/AnalyticsHub"));
const AdminHub = lazy(() => import("../../components/AdminHub"));
const SettingsPage = lazy(() => import("../../components/SettingsPage"));
const ProfilePage = lazy(() => import("../../components/ProfilePage"));
const CustomersPage = lazy(
  () => import("../../components/commerce/CustomersListPage"),
);
const CustomerDetailPage = lazy(
  () => import("../../components/commerce/CustomerDetailPage"),
);
const DriversHub = lazy(() => import("../../components/DriversHub"));
const DispatchCockpit = lazy(() => import("../../components/DispatchCockpit"));
const SetupHub = lazy(() => import("../../components/SetupHub"));
const OperationsControl = lazy(() => import("../ops/control/page"));
// In-shell orders board + detail (replaces routing out to /ops and /orders/:id
// so the dashboard keeps a single, consistent in-shell navigation model).
const OrdersBoard = lazy(() => import("../../components/ops/OrdersPage"));
const OrderDetailView = lazy(
  () => import("../../components/orders/OrderDetailView"),
);
const CreateOrderModal = lazy(
  () => import("../../components/ops/CreateOrderModal"),
);

function MapLoadingPlaceholder() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <div className="text-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary mx-auto"></div>
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
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [activeMenuItem, setActiveMenuItem] = useState(() => {
    if (typeof window !== "undefined") {
      return sessionStorage.getItem("activeMenuItem") || "today";
    }
    return "today";
  });
  const [selectedTruck, setSelectedTruck] = useState<Truck | null>(null);
  const [aiChatOpen, setAiChatOpen] = useState(false);
  const [orderDetailId, setOrderDetailId] = useState<string | null>(null);
  const [customerDetailId, setCustomerDetailId] = useState<string | null>(null);
  const [ordersQuery, setOrdersQuery] = useState("");
  const [createOrderOpen, setCreateOrderOpen] = useState(false);

  // Persist active menu item across refreshes
  const handleNavigate = (item: string) => {
    setActiveMenuItem(item);
    if (typeof window !== "undefined") {
      sessionStorage.setItem("activeMenuItem", item);
    }
  };

  // In-shell navigation helpers — keep order workflows inside the dashboard
  // shell instead of routing out to /ops and /orders/:id.
  const openOrder = (orderId: string) => {
    setOrderDetailId(orderId);
    handleNavigate("order-detail");
  };
  const openOrders = (query = "") => {
    setOrdersQuery(query);
    handleNavigate("orders");
  };
  const openCustomer = (customerId: string) => {
    setCustomerDetailId(customerId);
    handleNavigate("customer-detail");
  };

  // Cross-module reference links (EntityLink + inline links) open in-shell for
  // the entity types the shell can host; everything else falls back to a
  // canonical-route <Link> (handled inside EntityLink when handles() is false).
  const inShellNav = {
    handles: (type: string) => type === "order" || type === "customer",
    open: (type: string, id: string) => {
      if (type === "order") openOrder(id);
      else if (type === "customer") openCustomer(id);
    },
  };

  useEffect(() => {
    // Auth is now backed by a verified SuperTokens session (cookie-managed by
    // the SDK), not a sessionStorage flag. Check whether a session exists and
    // bounce to sign-in if not.
    let cancelled = false;
    (async () => {
      try {
        const exists = await Session.doesSessionExist();
        if (cancelled) return;
        if (exists) {
          setIsAuthenticated(true);
        } else {
          router.replace("/signin");
        }
      } catch {
        if (!cancelled) router.replace("/signin");
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  const handleTruckSelect = (truck: Truck) => {
    setSelectedTruck(truck);
  };

  const renderMainContent = () => {
    switch (activeMenuItem) {
      case "today":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Today">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <DispatchCockpit
                  onNavigate={handleNavigate}
                  onOpenOrder={openOrder}
                />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "orders":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Orders">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <OrdersBoard
                  onOrderClick={openOrder}
                  onCreateOrder={() => setCreateOrderOpen(true)}
                  initialQuery={ordersQuery}
                />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "order-detail":
        // Refreshing into this state loses the selected id — fall back to the
        // board rather than render a blank detail page.
        if (!orderDetailId) {
          return (
            <div className="flex-1 bg-gray-50">
              <ErrorBoundary componentName="Orders">
                <Suspense fallback={<ComponentLoadingPlaceholder />}>
                  <OrdersBoard
                    onOrderClick={openOrder}
                    onCreateOrder={() => setCreateOrderOpen(true)}
                    initialQuery={ordersQuery}
                  />
                </Suspense>
              </ErrorBoundary>
            </div>
          );
        }
        return (
          <div className="flex-1 overflow-auto bg-gray-50">
            <ErrorBoundary componentName="Order">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <OrderDetailView
                  orderId={orderDetailId}
                  onBack={() => handleNavigate("orders")}
                />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "fleet":
        return (
          <Suspense fallback={<ComponentLoadingPlaceholder />}>
            <FleetDashboard
              selectedTruck={selectedTruck}
              onTruckSelect={handleTruckSelect}
              mapView={<MapView selectedTruck={selectedTruck} />}
              onNavigate={handleNavigate}
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
                <CustomersPage onSelectCustomer={openCustomer} />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "customer-detail":
        // Refreshing into this state loses the selected id — fall back to the
        // customers list rather than render a blank detail page.
        if (!customerDetailId) {
          return (
            <div className="flex-1 bg-gray-50">
              <ErrorBoundary componentName="Customers">
                <Suspense fallback={<ComponentLoadingPlaceholder />}>
                  <CustomersPage onSelectCustomer={openCustomer} />
                </Suspense>
              </ErrorBoundary>
            </div>
          );
        }
        return (
          <div className="flex-1 overflow-auto bg-gray-50">
            <ErrorBoundary componentName="Customer">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <CustomerDetailPage
                  customerId={customerDetailId}
                  onBack={() => handleNavigate("customers")}
                />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "drivers":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Drivers">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <DriversHub />
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

      case "compliance":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Compliance">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <ComplianceHub />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "billing":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Billing">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <CommerceHub />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "reconciliation":
        // Reconciliation now lives as a tab inside the Billing & Commerce hub
        // (finance/back-office). Redirect any persisted "reconciliation" nav
        // state there so old sessions don't land on a blank screen.
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Billing">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <CommerceHub />
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

      case "control":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Control Center">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <OperationsControl />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "admin":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Admin">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <AdminHub />
              </Suspense>
            </ErrorBoundary>
          </div>
        );

      case "setup":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Setup">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <SetupHub />
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

      case "profile":
        return (
          <div className="flex-1 bg-gray-50">
            <ErrorBoundary componentName="Profile">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <ProfilePage />
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
          <div className="w-8 h-8 border-4 border-gray-300 border-t-primary rounded-full animate-spin mx-auto mb-4"></div>
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
          isMobileOpen={mobileSidebarOpen}
          onMobileClose={() => setMobileSidebarOpen(false)}
        />
        <div
          className="flex-1 flex flex-col min-h-0 overflow-hidden"
          style={{ minWidth: 0 }}
        >
          <Header
            onAIClick={() => setAiChatOpen(true)}
            onMenuClick={() => setMobileSidebarOpen(true)}
            onSearch={openOrders}
            onNewOrder={() => {
              setCreateOrderOpen(true);
              handleNavigate("orders");
            }}
          />
          <main className="flex-1 flex bg-white relative z-0 overflow-hidden">
            <InShellNavProvider value={inShellNav}>
              <div className="flex-1 flex bg-white overflow-auto">
                {renderMainContent()}
              </div>
            </InShellNavProvider>
          </main>
        </div>
      </div>
      <ErrorBoundary componentName="AI Chat">
        <Suspense fallback={null}>
          <AIChat isOpen={aiChatOpen} onClose={() => setAiChatOpen(false)} />
        </Suspense>
      </ErrorBoundary>
      <ErrorBoundary componentName="Create Order">
        <Suspense fallback={null}>
          <CreateOrderModal
            isOpen={createOrderOpen}
            onClose={() => setCreateOrderOpen(false)}
            onSuccess={(orderId) => {
              setCreateOrderOpen(false);
              openOrder(orderId);
            }}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
