"use client";

import {
  BookOpen,
  Building2,
  CreditCard,
  DollarSign,
  FileText,
  TrendingUp,
} from "lucide-react";
import { lazy, Suspense, useState } from "react";
import AccountsListPage from "./commerce/AccountsListPage";
import InvoicesListPage from "./commerce/InvoicesListPage";
import PriceBookEditor from "./commerce/PriceBookEditor";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const ARAgingDashboard = lazy(() => import("./commerce/ARAgingDashboard"));

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

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="Billing & Commerce"
        icon={<DollarSign className="w-5 h-5" />}
      />
      <TabNavigation
        tabs={TABS}
        activeTab={activeTab}
        onChange={setActiveTab}
      />
      <div className="flex-1 overflow-auto">
        {activeTab === "accounts" && <AccountsListPage />}
        {activeTab === "invoices" && <InvoicesListPage />}
        {activeTab === "price-books" && <PriceBookEditor />}
        {activeTab === "payments" && (
          <div className="p-6">
            <p className="text-gray-500 text-sm">
              Payments are shown on each invoice detail. Use the Invoices tab to
              view payment history per invoice.
            </p>
          </div>
        )}
        {activeTab === "ar-aging" && (
          <Suspense fallback={<LoadingSpinner message="Loading AR Aging..." />}>
            <ARAgingDashboard />
          </Suspense>
        )}
      </div>
    </div>
  );
}
