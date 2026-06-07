"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const FuelOpsPage = lazy(() => import("../../../components/FuelOpsPage"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function FuelOpsPageRoute() {
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Fuel Ops">
        <Suspense fallback={<Loading />}>
          <FuelOpsPage />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
