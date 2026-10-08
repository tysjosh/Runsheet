"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const OrdersBoard = lazy(() => import("../../../components/ops/OrdersPage"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function OrdersPageRoute() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const initialQuery = searchParams.get("q") ?? "";

  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Orders">
        <Suspense fallback={<Loading />}>
          <OrdersBoard
            onOrderClick={(id) =>
              router.push(`/dashboard/orders/${encodeURIComponent(id)}`)
            }
            initialQuery={initialQuery}
            initialStatus={searchParams.get("status") ?? ""}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
