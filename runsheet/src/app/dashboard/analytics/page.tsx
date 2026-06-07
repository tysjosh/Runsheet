"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const AnalyticsHub = lazy(() => import("../../../components/AnalyticsHub"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function AnalyticsPageRoute() {
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Analytics">
        <Suspense fallback={<Loading />}>
          <AnalyticsHub />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
