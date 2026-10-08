"use client";

import { useRouter } from "next/navigation";
import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const CustomersHub = lazy(
  () => import("../../../components/customers/CustomersHub"),
);

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function CustomersPageRoute() {
  const router = useRouter();
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Customers">
        <Suspense fallback={<Loading />}>
          <CustomersHub
            onSelectCustomer={(id) =>
              router.push(`/dashboard/customers/${encodeURIComponent(id)}`)
            }
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
