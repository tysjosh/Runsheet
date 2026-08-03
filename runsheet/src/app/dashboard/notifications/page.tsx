"use client";

/**
 * Notifications route. The hub itself lives in `components/NotificationsHub`
 * because an App Router `page.tsx` may not carry arbitrary named exports, and the
 * hub has to export `TABS` for the registry drift guard in
 * `config/modules.test.ts` to check the real array rather than a copy.
 */

import { lazy, Suspense } from "react";
import ErrorBoundary from "../../../components/ErrorBoundary";
import LoadingSpinner from "../../../components/LoadingSpinner";

const NotificationsHub = lazy(
  () => import("../../../components/NotificationsHub"),
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
          <NotificationsHub />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
