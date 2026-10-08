"use client";

import {
  BookOpen,
  Building2,
  CreditCard,
  FileText,
  ListChecks,
  Shield,
  Sliders,
  TrendingUp,
} from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { canSee } from "../config/modules";
import {
  getMarginAvailability,
  getOpenMarginAlertCount,
  type MarginAvailability,
} from "../services/marginApi";
import AccountsListPage from "./commerce/AccountsListPage";
import InvoicesListPage from "./commerce/InvoicesListPage";
import PaymentsListPage from "./commerce/PaymentsListPage";
import PriceBookEditor from "./commerce/PriceBookEditor";
import LoadingSpinner from "./LoadingSpinner";
import { useHubTabs } from "./shell/useHubTabs";
import {
  LoadErrorState,
  PageChromeProvider,
  PageHeader,
  type Tab,
  TabPanel,
} from "./ui";

const ARAgingDashboard = lazy(() => import("./commerce/ARAgingDashboard"));
// Price-protection contracts and pricing rules are commercial features
// (they consume the ``/commerce/*`` endpoints), so they live in the
// Commerce hub even though the page components are physically under
// ``components/compliance/``.
const PriceProtectionContractsPage = lazy(
  () => import("./compliance/PriceProtectionContractsPage"),
);
const PricingRulesPage = lazy(() => import("./compliance/PricingRulesPage"));
// Reconciliation is the four-way gallon-variance dashboard (ordered → loaded →
// delivered → invoiced). It's finance/back-office and ties into invoicing, so
// it lives in the Commerce hub beside AR Aging rather than as its own
// top-level sidebar destination.
const ReconciliationPage = lazy(() => import("./ops/ReconciliationPage"));
// Cost and margin (margin-feed): tenant admin only, gated by canSee("margin").
const MarginHub = lazy(() => import("./commerce/margin/MarginHub"));

/** The open-alert badge refreshes this often while the page is visible. */
export const MARGIN_BADGE_REFRESH_MS = 5 * 60 * 1000;

// Exported for the registry drift guard in `config/modules.test.ts`.
export const TABS: Tab[] = [
  {
    id: "accounts",
    label: "Accounts",
    icon: <Building2 className="w-4 h-4" />,
  },
  { id: "invoices", label: "Invoices", icon: <FileText className="w-4 h-4" /> },
  {
    id: "price-books",
    label: "Price Books",
    icon: <BookOpen className="w-4 h-4" />,
  },
  {
    id: "pricing-rules",
    label: "Pricing Rules",
    icon: <Sliders className="w-4 h-4" />,
  },
  {
    id: "contracts",
    label: "Contracts",
    icon: <Shield className="w-4 h-4" />,
  },
  {
    id: "payments",
    label: "Payments",
    icon: <CreditCard className="w-4 h-4" />,
  },
  {
    id: "ar-aging",
    label: "AR Aging",
    icon: <TrendingUp className="w-4 h-4" />,
  },
  {
    id: "reconciliation",
    label: "Reconciliation",
    icon: <ListChecks className="w-4 h-4" />,
  },
  {
    id: "margin",
    label: "Margin",
    icon: <TrendingUp className="w-4 h-4" />,
  },
];

function MarginAlertBadge({ count }: { count: number }) {
  const label = `${count} open margin alert${count === 1 ? "" : "s"}`;
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className="ml-1 rounded-full bg-red-100 px-1.5 text-xs font-semibold text-red-800"
    >
      {count}
    </span>
  );
}

export interface CommerceHubProps {
  /** Deep-linked tab (`?tab=`); falls back to the first visible tab. */
  initialTab?: string;
}

/**
 * Shown in place of the Margin tab's content when `?tab=margin` is opened but
 * the tab isn't offered: the feed is off for the tenant (no Try again: there
 * is nothing to retry), or the caller isn't a tenant admin (the standard
 * no-access state).
 */
export function MarginUnavailable({
  reason,
}: {
  reason: "disabled" | "forbidden";
}) {
  return (
    <div className="p-4">
      <LoadErrorState
        embedded
        entityLabel="Margin page"
        homeHref="/dashboard/billing?tab=invoices"
        homeLabel="Go to Invoices"
        failure={
          reason === "disabled"
            ? {
                kind: "module_disabled",
                moduleName: "Margin",
                status: 404,
                code: "COMMERCE_DISABLED",
                message: "Margin isn't turned on for this account.",
              }
            : {
                kind: "forbidden",
                status: 403,
                message: "Margin is for tenant admins.",
                details: { required_roles: ["admin"] },
              }
        }
      />
    </div>
  );
}

export default function CommerceHub({ initialTab }: CommerceHubProps = {}) {
  // Margin availability: probed once for admins (see getMarginAvailability).
  // "disabled" hides the tab; until the probe answers the tab stays hidden so
  // it doesn't flash in and out.
  const [marginAvailability, setMarginAvailability] = useState<
    MarginAvailability | "pending"
  >("pending");
  const {
    roles,
    tabs: allowedTabs,
    active: effectiveTab,
    setActive,
  } = useHubTabs(TABS, {
    fallback: initialTab,
    visible: (tab, r) =>
      canSee(tab.id, { roles: r }) &&
      (tab.id !== "margin" ||
        marginAvailability === "enabled" ||
        marginAvailability === "unknown"),
  });
  const rawTab = useSearchParams()?.get("tab") ?? initialTab ?? null;
  // Rows open the detail routes (task 1.10): the detail template has its own
  // title row with Back, so it doesn't sit under the hub's tabs.
  const router = useRouter();
  const handleSelectAccount = (accountId: string) =>
    router.push(`/dashboard/billing/accounts/${encodeURIComponent(accountId)}`);
  const handleSelectInvoice = (invoiceId: string) =>
    router.push(`/dashboard/billing/invoices/${encodeURIComponent(invoiceId)}`);

  const marginAllowed = canSee("margin", { roles });
  useEffect(() => {
    if (!marginAllowed) return;
    let cancelled = false;
    void getMarginAvailability().then((a) => {
      if (!cancelled) setMarginAvailability(a);
    });
    return () => {
      cancelled = true;
    };
  }, [marginAllowed]);
  const marginVisible =
    marginAllowed &&
    (marginAvailability === "enabled" || marginAvailability === "unknown");
  // `?tab=margin` that can't be honoured: say why instead of silently
  // showing the first tab.
  const marginBlocked: "disabled" | "forbidden" | "pending" | null =
    rawTab !== "margin" || roles === null
      ? null
      : !marginAllowed || marginAvailability === "forbidden"
        ? "forbidden"
        : marginAvailability === "disabled"
          ? "disabled"
          : marginAvailability === "pending"
            ? "pending"
            : null;
  const [openMarginAlerts, setOpenMarginAlerts] = useState(0);
  const refreshMarginAlerts = useCallback(async () => {
    try {
      setOpenMarginAlerts(await getOpenMarginAlertCount());
    } catch {
      // The badge is a convenience; the Alerts sub-tab reports errors.
    }
  }, []);

  useEffect(() => {
    if (!marginVisible) return;
    void refreshMarginAlerts();
    const id = setInterval(() => {
      if (
        typeof document === "undefined" ||
        document.visibilityState === "visible"
      ) {
        void refreshMarginAlerts();
      }
    }, MARGIN_BADGE_REFRESH_MS);
    return () => clearInterval(id);
  }, [marginVisible, refreshMarginAlerts]);

  const visibleTabs = allowedTabs.map((tab) =>
    tab.id === "margin" && openMarginAlerts > 0
      ? { ...tab, badge: <MarginAlertBadge count={openMarginAlerts} /> }
      : tab,
  );
  // Accounts is Tier 4, so it can be hidden while the hub itself stays visible
  // for Invoices and Reconciliation; `useHubTabs` falls back to the first
  // visible tab rather than an empty pane.
  const shows = (id: string) =>
    !marginBlocked &&
    effectiveTab === id &&
    allowedTabs.some((t) => t.id === id);

  return (
    <PageChromeProvider>
      <div className="flex flex-col h-full">
        <PageHeader
          host
          title="Billing"
          help="Accounts, invoices, pricing, and receivables"
          tabs={visibleTabs}
          tab={effectiveTab}
          onTabChange={setActive}
          tabIdBase="billing"
        />
        <TabPanel
          idBase="billing"
          value={effectiveTab}
          className="flex-1 overflow-auto"
        >
          {marginBlocked === "pending" && (
            <LoadingSpinner message="Loading margin..." />
          )}
          {(marginBlocked === "disabled" || marginBlocked === "forbidden") && (
            <MarginUnavailable reason={marginBlocked} />
          )}
          {shows("accounts") && (
            <AccountsListPage onSelectAccount={handleSelectAccount} />
          )}
          {shows("invoices") && (
            <InvoicesListPage onSelectInvoice={handleSelectInvoice} />
          )}
          {shows("price-books") && <PriceBookEditor />}
          {shows("pricing-rules") && (
            <Suspense
              fallback={<LoadingSpinner message="Loading pricing rules..." />}
            >
              <PricingRulesPage />
            </Suspense>
          )}
          {shows("contracts") && (
            <Suspense
              fallback={<LoadingSpinner message="Loading contracts..." />}
            >
              <PriceProtectionContractsPage />
            </Suspense>
          )}
          {shows("payments") && <PaymentsListPage />}
          {shows("ar-aging") && (
            <Suspense
              fallback={<LoadingSpinner message="Loading AR Aging..." />}
            >
              <ARAgingDashboard />
            </Suspense>
          )}
          {shows("reconciliation") && (
            <Suspense
              fallback={<LoadingSpinner message="Loading reconciliation..." />}
            >
              <ReconciliationPage />
            </Suspense>
          )}
          {shows("margin") && (
            <Suspense fallback={<LoadingSpinner message="Loading margin..." />}>
              <MarginHub onAlertsChanged={refreshMarginAlerts} />
            </Suspense>
          )}
        </TabPanel>
      </div>
    </PageChromeProvider>
  );
}
