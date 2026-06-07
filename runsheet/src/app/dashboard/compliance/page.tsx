"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const ComplianceHub = lazy(() => import("../../../components/ComplianceHub"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function CompliancePageRoute() {
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Compliance">
        <Suspense fallback={<Loading />}>
          <ComplianceHub />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
