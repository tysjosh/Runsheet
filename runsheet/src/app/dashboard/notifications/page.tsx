"use client";

/**
 * Notifications route — a top-level home for the notification history that was
 * previously buried under Settings › Support › Notifications. Reuses the
 * self-contained NotificationHistoryTab (summary bar, paginated table, search,
 * filters, detail panel, retry, live WebSocket updates).
 */

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const NotificationHistoryTab = lazy(
  () => import("../../../components/NotificationHistoryTab"),
);

export default function NotificationsPageRoute() {
  return (
    <div className="flex-1 flex bg-white">
      <ErrorBoundary componentName="Notifications">
        <Suspense
          fallback={
            <div className="flex-1 flex items-center justify-center">
              <LoadingSpinner message="Loading notifications..." />
            </div>
          }
        >
          <NotificationHistoryTab />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
