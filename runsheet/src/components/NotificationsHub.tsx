"use client";

/**
 * NotificationsHub — the single home for customer communications.
 *
 * History moved out of Settings › Support › Notifications first; Settings (rules,
 * templates, per-customer preferences) has now followed it, and the Support hub
 * that used to contain both is gone. Support's third tab was a ticketing UI for
 * the legacy Nigerian last-mile CRM: its only implemented endpoint sits behind
 * `LEGACY_NG_DELIVERY_ENABLED` (false in development, test, example and
 * production — audit 2026-05-08 recommendation #1), and create, detail and update
 * were never built, so `POST /api/support/tickets` answers 405.
 *
 * This lives in `components/` rather than in the route file because a Next.js App
 * Router `page.tsx` may not carry arbitrary named exports, and `TABS` has to be
 * exported for the registry drift guard in `config/modules.test.ts` to check the
 * real array instead of a copy.
 *
 * Both tabs are role-gated through `canSee` like every other hub, even though the
 * `notifications` nav item that leads here already requires the same two roles.
 * The redundancy is cheap; the alternative is one tab bar whose visibility rule
 * lives somewhere different from all the others.
 */

import { Bell, Settings as SettingsIcon } from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import { canSee, visibleByCanSee } from "../config/modules";
import { getCurrentUserRoles } from "../utils/auth";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const NotificationHistoryTab = lazy(() => import("./NotificationHistoryTab"));
const NotificationSettingsTab = lazy(() => import("./NotificationSettingsTab"));

/**
 * Exported so `config/modules.test.ts` can drift-guard the real array: `canSee`
 * returns false for an unregistered id, so an unnoticed typo here would silently
 * delete a tab rather than fail loudly.
 */
export const TABS: Tab[] = [
  {
    id: "notification-history",
    label: "History",
    icon: <Bell className="w-4 h-4" />,
  },
  {
    id: "notification-settings",
    label: "Settings",
    icon: <SettingsIcon className="w-4 h-4" />,
  },
];

type TabId = string;

export default function NotificationsHub() {
  const [activeTab, setActiveTab] = useState<TabId>("notification-history");
  // `null` until the session resolves; `canSee` treats that as no roles, so a
  // gated tab never flashes visible before we know.
  const [roles, setRoles] = useState<readonly string[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const r = await getCurrentUserRoles();
      if (!cancelled) setRoles(r);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const visibleTabs = visibleByCanSee(TABS, { roles });
  // Keep the selection on a tab the user can actually see — a hidden tab left
  // selected renders an empty pane under a tab bar that no longer offers it.
  const effectiveTab =
    visibleTabs.some((t) => t.id === activeTab) || visibleTabs.length === 0
      ? activeTab
      : visibleTabs[0].id;
  const shows = (id: string) => effectiveTab === id && canSee(id, { roles });

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="Notifications" />
      <TabNavigation
        tabs={visibleTabs}
        activeTab={effectiveTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 flex overflow-auto">
        <Suspense
          fallback={
            <div className="flex-1 flex items-center justify-center">
              <LoadingSpinner message="Loading notifications..." />
            </div>
          }
        >
          {shows("notification-history") && <NotificationHistoryTab />}
          {shows("notification-settings") && <NotificationSettingsTab />}
        </Suspense>
      </div>
    </div>
  );
}
