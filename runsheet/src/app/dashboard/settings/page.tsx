"use client";

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const SettingsPage = lazy(() => import("../../../components/SettingsPage"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function SettingsPageRoute() {
  return (
    <div className="flex-1 bg-gray-50">
      <ErrorBoundary componentName="Settings">
        <Suspense fallback={<Loading />}>
          <SettingsPage />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
