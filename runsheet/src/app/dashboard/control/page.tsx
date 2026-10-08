"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

// Live (labelled "Live" in the nav; route id `control`).
const OperationsControl = lazy(
  () => import("../../../components/ops/OperationsControlPage"),
);

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
          <OperationsControl />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
