"use client";

/**
 * Settings (UI revamp R10.4, design.md §4 "Settings tabs"): one home for what
 * used to be Setup and Admin, plus notification rules and templates.
 *
 * Settings template: a 240 px section list on the left, the section on the
 * right; the section is `?tab=`. Every section keeps the gate it had before
 * the merge. Setup sections need `setup` (admin + dispatcher); Admin sections
 * need `admin` (admin only) on top of their own id, so a dispatcher sees
 * exactly what Setup showed them before and nothing from Admin.
 *
 * The section pages still render their own `PageHeader`; inside this page's
 * `PageChromeProvider` those collapse into the Settings title row, so the page
 * has one `<h1>` and one header row.
 */
import { lazy, Suspense, useMemo } from "react";
import { canSee } from "../../config/modules";
import LoadingSpinner from "../LoadingSpinner";
import { useSessionRoles } from "../shell/useHubTabs";
import { PageChromeProvider, PageHeader, useUrlTab } from "../ui";

const DepotsPage = lazy(() => import("../admin/DepotsPage"));
const RoadRestrictionsPanel = lazy(
  () => import("../admin/RoadRestrictionsPanel"),
);
const WeatherAlertsPage = lazy(() => import("../admin/WeatherAlertsPage"));
const TaxJurisdictionsPage = lazy(
  () => import("../compliance/TaxJurisdictionsPage"),
);
const ExemptionsPage = lazy(() => import("../compliance/ExemptionsPage"));
const NotificationSettingsTab = lazy(
  () => import("../NotificationSettingsTab"),
);
const IntegrationMarketplacePage = lazy(
  () => import("../admin/IntegrationMarketplacePage"),
);
const IntakeChannelsAdminPanel = lazy(
  () => import("../admin/IntakeChannelsAdminPanel"),
);
const StripeIntegrationUI = lazy(() => import("../admin/StripeIntegrationUI"));
const AgentSettingsPage = lazy(() => import("../ops/AgentSettingsPage"));
const AgentMonitoringDashboard = lazy(
  () => import("../admin/AgentMonitoringDashboard"),
);
const DataImport = lazy(() => import("../DataImport"));
const FeatureFlagsAdmin = lazy(() => import("../admin/FeatureFlagsAdmin"));
const NotificationMetricsDashboard = lazy(
  () => import("../admin/NotificationMetricsDashboard"),
);
const SystemHealthPanel = lazy(() => import("../admin/SystemHealthPanel"));

export interface SettingsSection {
  /** URL value (`?tab=`). */
  id: string;
  /** Registry id checked by `canSee`. */
  moduleId: string;
  /** Container gate the section inherited from its old hub. */
  gate: "setup" | "admin" | "notifications";
  label: string;
  group: string;
}

// Exported for the registry drift guard in `config/modules.test.ts`.
export const SETTINGS_SECTIONS: SettingsSection[] = [
  {
    id: "company",
    moduleId: "depots",
    gate: "setup",
    label: "Depots",
    group: "Company",
  },
  {
    id: "road-restrictions",
    moduleId: "road-restrictions",
    gate: "setup",
    label: "Road restrictions",
    group: "Company",
  },
  {
    id: "weather-alerts",
    moduleId: "weather-alerts",
    gate: "admin",
    label: "Weather alerts",
    group: "Company",
  },
  {
    id: "tax",
    moduleId: "tax",
    gate: "setup",
    label: "Tax jurisdictions",
    group: "Company",
  },
  {
    id: "exemptions",
    moduleId: "exemptions",
    gate: "setup",
    label: "Exemptions",
    group: "Company",
  },
  {
    id: "notifications",
    moduleId: "notification-settings",
    gate: "notifications",
    label: "Rules and templates",
    group: "Notifications",
  },
  {
    id: "integrations",
    moduleId: "integrations",
    gate: "admin",
    label: "Marketplace",
    group: "Integrations",
  },
  {
    id: "intake-channels",
    moduleId: "intake-channels",
    gate: "admin",
    label: "Intake channels",
    group: "Integrations",
  },
  {
    id: "stripe",
    moduleId: "stripe",
    gate: "admin",
    label: "Stripe",
    group: "Integrations",
  },
  {
    id: "agents",
    moduleId: "agent-settings",
    gate: "admin",
    label: "Agent settings",
    group: "Agents",
  },
  {
    id: "agent-monitoring",
    moduleId: "agents",
    gate: "admin",
    label: "Agent monitoring",
    group: "Agents",
  },
  {
    id: "import",
    moduleId: "import",
    gate: "admin",
    label: "Data import",
    group: "Data",
  },
  {
    id: "flags",
    moduleId: "feature-flags",
    gate: "admin",
    label: "Feature flags",
    group: "Data",
  },
  {
    id: "metrics",
    moduleId: "metrics",
    gate: "admin",
    label: "Notification metrics",
    group: "Data",
  },
  // Poison-queue depth for Runsheet staff (D12, task 3.9). `system-health`
  // requires platform_admin; the `admin` gate keeps it inside Settings' admin
  // set (platform_admin is held alongside a tenant role).
  {
    id: "system",
    moduleId: "system-health",
    gate: "admin",
    label: "System health",
    group: "Platform",
  },
];

/** Old tab ids (Setup, Admin, design.md §4) that still resolve. */
export const SETTINGS_ALIASES: Record<string, string> = {
  depots: "company",
  setup: "company",
  "feature-flags": "flags",
  "agent-settings": "agents",
  "notification-settings": "notifications",
  "system-health": "system",
};

export function sectionVisible(
  s: SettingsSection,
  roles: readonly string[] | null,
): boolean {
  return canSee(s.gate, { roles }) && canSee(s.moduleId, { roles });
}

export default function SettingsPage() {
  const roles = useSessionRoles();
  const visible = useMemo(
    () => SETTINGS_SECTIONS.filter((s) => sectionVisible(s, roles)),
    [roles],
  );
  const [active, setActive] = useUrlTab(
    visible.map((s) => s.id),
    { aliases: SETTINGS_ALIASES },
  );
  const section = visible.find((s) => s.id === active);
  const groups = visible.reduce<Record<string, SettingsSection[]>>((acc, s) => {
    if (!acc[s.group]) acc[s.group] = [];
    acc[s.group].push(s);
    return acc;
  }, {});

  return (
    <PageChromeProvider>
      <div className="flex h-full flex-col">
        <PageHeader
          host
          title="Settings"
          context={
            section ? (
              <span className="text-sm text-text-muted">
                {section.group} · {section.label}
              </span>
            ) : undefined
          }
        />
        <div className="flex min-h-0 flex-1">
          <nav
            aria-label="Settings sections"
            className="w-60 shrink-0 overflow-y-auto border-r border-slate-200 bg-surface px-2 py-3"
          >
            {Object.entries(groups).map(([group, items]) => (
              <div key={group} className="mb-3">
                <p className="px-2.5 pb-1 text-[11px] font-semibold uppercase tracking-wider text-slate-600">
                  {group}
                </p>
                <ul className="space-y-0.5">
                  {items.map((s) => (
                    <li key={s.id}>
                      <button
                        type="button"
                        onClick={() => setActive(s.id)}
                        aria-current={s.id === active ? "page" : undefined}
                        className={`flex h-8 w-full items-center rounded-lg px-2.5 text-left text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
                          s.id === active
                            ? "bg-primary-soft font-semibold text-brand-800"
                            : "text-slate-800 hover:bg-slate-100"
                        }`}
                      >
                        {s.label}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </nav>
          <section
            aria-label={section ? section.label : "Settings"}
            className="min-w-0 flex-1 overflow-auto bg-white"
          >
            <Suspense fallback={<LoadingSpinner message="Loading..." />}>
              {section && <SectionBody id={section.id} roles={roles} />}
            </Suspense>
          </section>
        </div>
      </div>
    </PageChromeProvider>
  );
}

function SectionBody({
  id,
  roles,
}: {
  id: string;
  roles: readonly string[] | null;
}) {
  switch (id) {
    case "company":
      return <DepotsPage />;
    case "road-restrictions":
      return <RoadRestrictionsPanel roles={roles ?? []} />;
    case "weather-alerts":
      return <WeatherAlertsPage />;
    case "tax":
      return <TaxJurisdictionsPage />;
    case "exemptions":
      return <ExemptionsPage />;
    case "notifications":
      return <NotificationSettingsTab />;
    case "integrations":
      return <IntegrationMarketplacePage />;
    case "intake-channels":
      return <IntakeChannelsAdminPanel />;
    case "stripe":
      return <StripeIntegrationUI />;
    case "agents":
      return <AgentSettingsPage />;
    case "agent-monitoring":
      return <AgentMonitoringDashboard />;
    case "import":
      return <DataImport />;
    case "flags":
      return <FeatureFlagsAdmin />;
    case "metrics":
      return <NotificationMetricsDashboard />;
    case "system":
      return <SystemHealthPanel />;
    default:
      return null;
  }
}
