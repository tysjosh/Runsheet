'use client';

import { useState, useEffect, lazy, Suspense } from 'react';
import { useRouter } from 'next/navigation';
import dynamic from 'next/dynamic';
import Header from '../components/Header';
import Sidebar from '../components/Sidebar';
import ErrorBoundary from '../components/ErrorBoundary';
import LoadingSpinner from '../components/LoadingSpinner';
import { Truck } from '../types/api';

// Lazy-load heavy components for code splitting
// These components are loaded on-demand when their routes are accessed

// MapView is lazy-loaded because it includes the heavy Google Maps library
const MapView = dynamic(() => import('../components/MapView'), {
  loading: () => <MapLoadingPlaceholder />,
  ssr: false, // Google Maps doesn't work with SSR
});

// Route-based lazy loading for main content components
const FleetTracking = lazy(() => import('../components/FleetTracking'));
const AIChat = lazy(() => import('../components/AIChat'));
const DataUpload = lazy(() => import('../components/DataUpload'));
const Inventory = lazy(() => import('../components/Inventory'));
const Orders = lazy(() => import('../components/Orders'));
const Analytics = lazy(() => import('../components/Analytics'));
const Support = lazy(() => import('../components/Support'));

// Loading placeholder for the map component
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

// Generic loading placeholder for lazy-loaded components
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
  const [activeMenuItem, setActiveMenuItem] = useState('fleet');
  const [selectedTruck, setSelectedTruck] = useState<Truck | null>(null);
  const [aiChatOpen, setAiChatOpen] = useState(false);

  // Check authentication on component mount
  useEffect(() => {
    const checkAuth = () => {
      const authStatus = localStorage.getItem('isAuthenticated');

      if (authStatus === 'true') {
        setIsAuthenticated(true);
        setIsLoading(false);
      } else {
        // Clear any existing auth data
        localStorage.removeItem('isAuthenticated');
        localStorage.removeItem('userEmail');

        // Redirect to signin
        router.replace('/signin');
        setIsLoading(false);
      }
    };

    // Check if we're in the browser environment
    if (typeof window !== 'undefined') {
      checkAuth();
    }
  }, [router]);

  const handleSidebarToggle = () => {
    setSidebarCollapsed(!sidebarCollapsed);
  };

  const handleMenuNavigation = (item: string) => {
    setActiveMenuItem(item.toLowerCase());
  };

  const handleTruckSelect = (truck: Truck) => {
    setSelectedTruck(truck);
  };

  const handleAIClick = () => {
    setAiChatOpen(true);
  };

  const renderMainContent = () => {
    switch (activeMenuItem) {
      case 'upload-data':
        return (
          <div className="flex-1 p-6 bg-gray-50">
            <div className="bg-white rounded-xl shadow-sm border border-gray-200 h-full overflow-hidden">
              <Suspense fallback={<ComponentLoadingPlaceholder />}>
                <DataUpload />
              </Suspense>
            </div>
          </div>
        );

      case 'inventory':
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

      case 'orders':
        return (
          <div className="flex-1 p-6 bg-gray-50">
            <div className="bg-white rounded-xl shadow-sm border border-gray-200 h-full overflow-hidden">
              <ErrorBoundary componentName="Orders">
                <Suspense fallback={<ComponentLoadingPlaceholder />}>
                  <Orders />
                </Suspense>
              </ErrorBoundary>
            </div>
          </div>
        );

      case 'fleet':
        return (
          <div className="flex-1 p-6 bg-gray-50">
            <div className="flex gap-6 h-full">
              {/* Fleet Tracking Panel */}
              <div className="w-1/2 bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
                <ErrorBoundary componentName="Fleet Tracking">
                  <Suspense fallback={<ComponentLoadingPlaceholder />}>
                    <FleetTracking onTruckSelect={handleTruckSelect} />
                  </Suspense>
                </ErrorBoundary>
              </div>

              {/* Map View - Lazy-loaded with dynamic import (includes Google Maps) */}
              <div className="w-1/2 bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
                <MapView selectedTruck={selectedTruck} />
              </div>
            </div>
          </div>
        );


      case 'analytics':
        return (
          <div className="flex-1 p-6 bg-gray-50">
            <div className="bg-white rounded-xl shadow-sm border border-gray-200 h-full overflow-hidden">
              <ErrorBoundary componentName="Analytics">
                <Suspense fallback={<ComponentLoadingPlaceholder />}>
                  <Analytics />
                </Suspense>
              </ErrorBoundary>
            </div>
          </div>
        );

      case 'support':
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
              <h2 className="text-xl font-semibold text-gray-700 mb-2">Welcome to RUNSHEET</h2>
              <p className="text-gray-500">Select a module from the sidebar to get started</p>
            </div>
          </div>
        );
    }
  };

  // Show loading spinner while checking authentication
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

  // Don't render main app if not authenticated
  if (!isAuthenticated) {
    return null;
  }

  return (
    <div className="h-screen flex flex-col bg-white">
      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar - Full Height (minus top bar) */}
        <Sidebar
          activeItem={activeMenuItem}
          isCollapsed={sidebarCollapsed}
          onToggle={handleSidebarToggle}
          onNavigate={handleMenuNavigation}
        />

        {/* Main Content Area */}
        <div className="flex-1 flex flex-col min-h-0 overflow-hidden" style={{ minWidth: 0 }}>
          <Header onAIClick={handleAIClick} />

          <main className="flex-1 flex bg-white relative z-0 overflow-hidden">
            <div className="flex-1 flex bg-white overflow-auto">
              {renderMainContent()}
            </div>
          </main>
        </div>
      </div>

      {/* AI Chat Overlay - Lazy-loaded */}
      <ErrorBoundary componentName="AI Chat">
        <Suspense fallback={null}>
          <AIChat
            isOpen={aiChatOpen}
            onClose={() => setAiChatOpen(false)}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
