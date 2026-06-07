"use client";

import { useSearchParams } from "next/navigation";
import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const AdminHub = lazy(() => import("../../../components/AdminHub"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function AdminPageRoute() {
  const searchParams = useSearchParams();
  // Deep-link a specific Admin tab via ?tab= (e.g. the Storm_Mode banner's
  // "Full details" opens Admin → Weather Alerts in-shell).
  const initialTab = searchParams.get("tab") ?? "metrics";

  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Admin">
        <Suspense fallback={<Loading />}>
          <AdminHub initialTab={initialTab} />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
