"use client";
import { lazy, Suspense } from "react";
import LoadingSpinner from "./LoadingSpinner";

const ReconciliationPage = lazy(() => import("./ops/ReconciliationPage"));

export default function ReconciliationHub() {
  return (
    <Suspense fallback={<LoadingSpinner message="Loading..." />}>
      <ReconciliationPage />
    </Suspense>
  );
}
