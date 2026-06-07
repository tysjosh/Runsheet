"use client";

import { useRouter } from "next/navigation";
import { lazy, Suspense } from "react";
import ErrorBoundary from "../../components/ErrorBoundary";
import LoadingSpinner from "../../components/LoadingSpinner";
import { dashboardPathForItem } from "./shell-context";

const DispatchCockpit = lazy(() => import("../../components/DispatchCockpit"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function TodayPage() {
  const router = useRouter();
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Today">
        <Suspense fallback={<Loading />}>
          <DispatchCockpit
            onNavigate={(item) => router.push(dashboardPathForItem(item))}
            onOpenOrder={(id) =>
              router.push(`/dashboard/orders/${encodeURIComponent(id)}`)
            }
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
