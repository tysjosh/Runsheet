"use client";

/**
 * Customers hub: Customers · Communications (UI revamp IA).
 *
 * Communications is the customer notification history that used to be the
 * Notifications nav item (`/dashboard/customers?tab=communications` redirects here with
 * `?tab=communications`). Notification rules and templates moved to
 * Settings → Notifications.
 */
import { Bell, Users } from "lucide-react";
import { lazy, Suspense } from "react";
import { canSee } from "../../config/modules";
import LoadingSpinner from "../LoadingSpinner";
import { useHubTabs } from "../shell/useHubTabs";
import { PageChromeProvider, PageHeader, type TabItem, TabPanel } from "../ui";

const CustomersListPage = lazy(() => import("../commerce/CustomersListPage"));
const NotificationHistoryTab = lazy(() => import("../NotificationHistoryTab"));

// Exported for the registry drift guard in `config/modules.test.ts`.
export const TABS: TabItem[] = [
  { id: "customers", label: "Customers", icon: <Users className="w-4 h-4" /> },
  {
    id: "communications",
    label: "Communications",
    icon: <Bell className="w-4 h-4" />,
  },
];

/** Registry gates per tab. */
export const TAB_GATES: Record<string, string[]> = {
  customers: ["customers"],
  communications: ["notifications", "notification-history"],
};

export default function CustomersHub({
  onSelectCustomer,
}: {
  onSelectCustomer: (id: string) => void;
}) {
  const { tabs, active, setActive, shows } = useHubTabs(TABS, {
    visible: (tab, roles) =>
      (TAB_GATES[tab.id] ?? [tab.id]).every((id) => canSee(id, { roles })),
  });
  return (
    <PageChromeProvider>
      <div className="flex flex-col h-full">
        <PageHeader
          host
          title="Customers"
          tabs={tabs}
          tab={active}
          onTabChange={setActive}
          tabIdBase="customers"
        />
        <TabPanel
          idBase="customers"
          value={active}
          className="flex-1 flex flex-col min-h-0 overflow-auto"
        >
          <Suspense fallback={<LoadingSpinner message="Loading..." />}>
            {shows("customers") && (
              <CustomersListPage onSelectCustomer={onSelectCustomer} />
            )}
            {shows("communications") && <NotificationHistoryTab />}
          </Suspense>
        </TabPanel>
      </div>
    </PageChromeProvider>
  );
}
