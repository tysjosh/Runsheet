"use client";

import {
  BookOpen,
  Building2,
  CreditCard,
  DollarSign,
  FileText,
  Shield,
  Sliders,
  TrendingUp,
} from "lucide-react";
import { lazy, Suspense, useState } from "react";
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

const TABS: Tab[] = [
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

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="Billing & Commerce"
        icon={<DollarSign className="w-5 h-5" />}
      />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={(tabId) => {
          setActiveTab(tabId);
          setSelectedAccountId(null); // Reset account selection when changing tabs
          setSelectedInvoiceId(null); // Reset invoice selection when changing tabs
        }}
      />
      <div className="flex-1 overflow-auto">
        {activeTab === "accounts" &&
          (selectedAccountId ? (
            <AccountDetailPage
              accountId={selectedAccountId}
              onBack={handleBackToAccountList}
            />
          ) : (
            <AccountsListPage onSelectAccount={handleSelectAccount} />
          ))}
        {activeTab === "invoices" &&
          (selectedInvoiceId ? (
            <InvoiceDetailPage
              invoiceId={selectedInvoiceId}
              onBack={handleBackToInvoiceList}
              onViewAccount={handleViewAccountFromInvoice}
            />
          ) : (
            <InvoicesListPage onSelectInvoice={handleSelectInvoice} />
          ))}
        {activeTab === "price-books" && <PriceBookEditor />}
        {activeTab === "pricing-rules" && (
          <Suspense
            fallback={<LoadingSpinner message="Loading pricing rules..." />}
          >
            <PricingRulesPage />
          </Suspense>
        )}
        {activeTab === "contracts" && (
          <Suspense
            fallback={<LoadingSpinner message="Loading contracts..." />}
          >
            <PriceProtectionContractsPage />
          </Suspense>
        )}
        {activeTab === "payments" && <PaymentsListPage />}
        {activeTab === "ar-aging" && (
          <Suspense fallback={<LoadingSpinner message="Loading AR Aging..." />}>
            <ARAgingDashboard />
          </Suspense>
        )}
      </div>
    </div>
  );
}
