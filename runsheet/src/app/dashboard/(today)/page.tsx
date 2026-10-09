"use client";
import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

// Home (UI revamp §7.1): Needs attention, Today's runs, Plan status.
const Dashboard = lazy(() => import("../../../components/dashboard/Dashboard"));

function Loading() {
  return (
    <div className="h-full flex items-center justify-center bg-canvas">
      <LoadingSpinner message="Loading..." />
    </div>
  );
}

export default function DashboardHomePage() {
  return (
    <div className="flex-1 bg-canvas">
      <ErrorBoundary componentName="Dashboard">
        <Suspense fallback={<Loading />}>
          <Dashboard />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
