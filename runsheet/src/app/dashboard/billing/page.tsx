"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const CommerceHub = lazy(() => import("../../../components/CommerceHub"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function BillingPageRoute() {
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Billing">
        <Suspense fallback={<Loading />}>
          <CommerceHub />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
