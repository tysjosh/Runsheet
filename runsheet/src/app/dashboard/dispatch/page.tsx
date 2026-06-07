"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const DispatchPage = lazy(() => import("../../../components/DispatchPage"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function DispatchPageRoute() {
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Dispatch">
        <Suspense fallback={<Loading />}>
          <DispatchPage />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
