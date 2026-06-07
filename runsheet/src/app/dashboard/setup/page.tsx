"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const SetupHub = lazy(() => import("../../../components/SetupHub"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function SetupPageRoute() {
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Setup">
        <Suspense fallback={<Loading />}>
          <SetupHub />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
