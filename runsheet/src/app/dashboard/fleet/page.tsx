"use client";

import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { lazy, Suspense, useState } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";
import type { Truck } from "../../../types/api";
import { dashboardPathForItem } from "../shell-context";

// MapView — heavy Google Maps library, no SSR.
const MapView = dynamic(() => import("../../../components/MapView"), {
  loading: () => <Loading message="Loading map..." />,
  ssr: false,
});

const FleetDashboard = lazy(() => import("../../../components/FleetDashboard"));

function Loading({ message = "Loading..." }: { message?: string }) {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message={message} />
    </div>
  );
}

export default function FleetPageRoute() {
  const router = useRouter();
  const [selectedTruck, setSelectedTruck] = useState<Truck | null>(null);

  return (
    <ErrorBoundary componentName="Fleet">
      <Suspense fallback={<Loading />}>
        <FleetDashboard
          selectedTruck={selectedTruck}
          onTruckSelect={setSelectedTruck}
          mapView={<MapView selectedTruck={selectedTruck} />}
          onNavigate={(item) => router.push(dashboardPathForItem(item))}
        />
      </Suspense>
    </ErrorBoundary>
  );
}
