"use client";

import {
  BookOpen,
  Building2,
  CreditCard,
  DollarSign,
  FileText,
  ListChecks,
  Shield,
  Sliders,
  TrendingUp,
} from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import { canSee, visibleByCanSee } from "../config/modules";
import { getCurrentUserRoles } from "../utils/auth";
import AccountDetailPage from "./commerce/AccountDetailPage";
import AccountsListPage from "./commerce/AccountsListPage";
import InvoiceDetailPage from "./commerce/InvoiceDetailPage";
import InvoicesListPage from "./commerce/InvoicesListPage";
import PaymentsListPage from "./commerce/PaymentsListPage";
import PriceBookEditor from "./commerce/PriceBookEditor";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

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
];

type TabId = string;

export default function CommerceHub() {
  const [activeTab, setActiveTab] = useState<TabId>("accounts");
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(
    null,
  );
  const [selectedInvoiceId, setSelectedInvoiceId] = useState<string | null>(
    null,
  );
  // `null` until resolved; `canSee` treats that as no roles.
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

  const handleSelectAccount = (accountId: string) => {
    setSelectedAccountId(accountId);
  };

  const handleSelectInvoice = (invoiceId: string) => {
    setSelectedInvoiceId(invoiceId);
  };

  const handleBackToAccountList = () => {
    setSelectedAccountId(null);
  };

  const handleBackToInvoiceList = () => {
    setSelectedInvoiceId(null);
  };

  // Invoice → Account is in-hub navigation: accounts live as a tab in this hub
  // rather than at a standalone route, so traversing an invoice's account
  // switches tabs and selects the account (Req 12.1).
  const handleViewAccountFromInvoice = (accountId: string) => {
    setSelectedInvoiceId(null);
    setSelectedAccountId(accountId);
    setActiveTab("accounts");
  };

  const visibleTabs = visibleByCanSee(TABS, { roles });
  // Accounts is Tier 4, so it can be hidden while the hub itself stays visible
  // for Invoices and Reconciliation. Fall back to the first visible tab rather
  // than rendering an empty pane under a tab bar that no longer offers it.
  const effectiveTab =
    visibleTabs.some((t) => t.id === activeTab) || visibleTabs.length === 0
      ? activeTab
      : visibleTabs[0].id;
  const shows = (id: string) => effectiveTab === id && canSee(id, { roles });

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="Billing & Commerce"
        subtitle="Accounts, invoices, pricing, and receivables"
        icon={<DollarSign className="w-5 h-5" />}
      />
      <TabNavigation
        tabs={visibleTabs}
        activeTab={effectiveTab}
        onChange={(tabId) => {
          setActiveTab(tabId);
          setSelectedAccountId(null); // Reset account selection when changing tabs
          setSelectedInvoiceId(null); // Reset invoice selection when changing tabs
        }}
      />
      <div className="flex-1 overflow-auto">
        {shows("accounts") &&
          (selectedAccountId ? (
            <AccountDetailPage
              accountId={selectedAccountId}
              onBack={handleBackToAccountList}
            />
          ) : (
            <AccountsListPage onSelectAccount={handleSelectAccount} />
          ))}
        {shows("invoices") &&
          (selectedInvoiceId ? (
            <InvoiceDetailPage
              invoiceId={selectedInvoiceId}
              onBack={handleBackToInvoiceList}
              onViewAccount={handleViewAccountFromInvoice}
            />
          ) : (
            <InvoicesListPage onSelectInvoice={handleSelectInvoice} />
          ))}
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
          <Suspense fallback={<LoadingSpinner message="Loading AR Aging..." />}>
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
      </div>
    </div>
  );
}
