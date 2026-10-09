"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

// Live (labelled "Live" in the nav; route id `control`), UI revamp §7.3.
const LiveView = lazy(() => import("../../../components/live/LiveView"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function ControlPageRoute() {
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Live">
        <Suspense fallback={<Loading />}>
          <LiveView />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
