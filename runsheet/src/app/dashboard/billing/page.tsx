"use client";

import { useSearchParams } from "next/navigation";
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
  const searchParams = useSearchParams();
  // Deep-link a tab via ?tab= (e.g. ?tab=margin). A tab the user may not see
  // falls back to the first visible one inside CommerceHub.
  const initialTab = searchParams.get("tab") ?? undefined;
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Billing">
        <Suspense fallback={<Loading />}>
          <CommerceHub initialTab={initialTab} />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
