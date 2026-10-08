"use client";

/**
 * Drivers: utilization and qualifications. Lives under Fleet → Drivers
 * (`/dashboard/fleet?tab=drivers`, UI revamp §4 row 20). Its two views are a
 * segmented control in Fleet's title row (`?view=`), so the page adds no
 * header or tab row of its own.
 */
import { Activity, FileText } from "lucide-react";
import { lazy, Suspense } from "react";
import LoadingSpinner from "./LoadingSpinner";
import { type TabItem, Tabs, usePageChrome, useUrlTab } from "./ui";

const DriverUtilizationView = lazy(
  () => import("./drivers/DriverUtilizationView"),
);
const DriverQualificationsView = lazy(
  () => import("./drivers/DriverQualificationsView"),
);

const VIEWS: TabItem[] = [
  {
    id: "utilization",
    label: "Utilization",
    icon: <Activity className="w-4 h-4" />,
  },
  {
    id: "qualifications",
    label: "Qualifications",
    icon: <FileText className="w-4 h-4" />,
  },
];

export default function DriversHub() {
  const [view, setView] = useUrlTab(
    VIEWS.map((v) => v.id),
    { param: "view" },
  );
  const switcher = (
    <Tabs tabs={VIEWS} value={view} onChange={setView} label="Driver views" />
  );
  const embedded = usePageChrome({ context: switcher });
  return (
    <div className="flex flex-col h-full">
      {!embedded && (
        <div className="flex h-11 items-center gap-3 border-b border-slate-200 bg-surface px-4">
          <h1 className="text-base font-semibold text-text">Drivers</h1>
          {switcher}
        </div>
      )}
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {view === "utilization" && <DriverUtilizationView />}
          {view === "qualifications" && <DriverQualificationsView />}
        </Suspense>
      </div>
    </div>
  );
}
