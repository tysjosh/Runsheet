"use client";

import { useParams, useRouter } from "next/navigation";
import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../../components/ErrorBoundary";
import LoadingSpinner from "../../../../components/LoadingSpinner";

const CustomerDetailPage = lazy(
  () => import("../../../../components/commerce/CustomerDetailPage"),
);

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function CustomerDetailRoute() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const customerId = params?.id ?? "";

  return (
    <div className="flex-1 overflow-auto bg-gray-50">
      <ErrorBoundary componentName="Customer">
        <Suspense fallback={<Loading />}>
          <CustomerDetailPage
            customerId={customerId}
            onBack={() => router.push("/dashboard/customers")}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
