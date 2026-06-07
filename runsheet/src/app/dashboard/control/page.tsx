"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

// Reuse the standalone Operations Control page component in-shell, as the
// original dashboard shell did.
const OperationsControl = lazy(() => import("../../ops/control/page"));

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
      <ErrorBoundary componentName="Control Center">
        <Suspense fallback={<Loading />}>
          <OperationsControl />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
