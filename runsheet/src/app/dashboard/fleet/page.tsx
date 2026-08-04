"use client";

import dynamic from "next/dynamic";
import { useRouter, useSearchParams } from "next/navigation";
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

/**
 * The Fleet hub body. Reads `?asset=<id>` — the canonical asset destination
 * produced by `entityHref("asset", id)` — and hands it down so the tracking
 * table selects (and the map focuses) that asset instead of the link landing
 * on an unchanged view.
 */
function FleetPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const focusAssetId = searchParams.get("asset");
  const [selectedTruck, setSelectedTruck] = useState<Truck | null>(null);

  return (
    <FleetDashboard
      selectedTruck={selectedTruck}
      onTruckSelect={setSelectedTruck}
      focusAssetId={focusAssetId}
      mapView={<MapView selectedTruck={selectedTruck} />}
      onNavigate={(item) => router.push(dashboardPathForItem(item))}
    />
  );
}

export default function FleetPageRoute() {
  return (
    <ErrorBoundary componentName="Fleet">
      {/* Suspense also covers `useSearchParams` in FleetPage. */}
      <Suspense fallback={<Loading />}>
        <FleetPage />
      </Suspense>
    </ErrorBoundary>
  );
}
