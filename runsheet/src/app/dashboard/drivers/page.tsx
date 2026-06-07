"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const DriversHub = lazy(() => import("../../../components/DriversHub"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function DriversPageRoute() {
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Drivers">
        <Suspense fallback={<Loading />}>
          <DriversHub />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
