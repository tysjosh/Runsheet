"use client";

import { useParams, useRouter } from "next/navigation";
import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../../components/ErrorBoundary";
import LoadingSpinner from "../../../../components/LoadingSpinner";

const OrderDetailView = lazy(
  () => import("../../../../components/orders/OrderDetailView"),
);

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function OrderDetailRoute() {
  const params = useParams<{ orderId: string }>();
  const router = useRouter();
  const orderId = params?.orderId ?? "";

  return (
    <div className="flex-1 overflow-auto bg-gray-50">
      <ErrorBoundary componentName="Order">
        <Suspense fallback={<Loading />}>
          <OrderDetailView
            orderId={orderId}
            onBack={() => router.push("/dashboard/orders")}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
