"use client";

import { useState } from "react";
import { DollarSign, Users, Building2, FileText, CreditCard, BookOpen } from "lucide-react";
import CustomersListPage from "../../components/commerce/CustomersListPage";
import AccountsListPage from "../../components/commerce/AccountsListPage";
import InvoicesListPage from "../../components/commerce/InvoicesListPage";
import PriceBookEditor from "../../components/commerce/PriceBookEditor";
// Payments tab uses InvoicesListPage filtered or a simple payments list

const TABS = [
  { id: "customers", label: "Customers", icon: Users },
  { id: "accounts", label: "Accounts", icon: Building2 },
  { id: "invoices", label: "Invoices", icon: FileText },
  { id: "price-books", label: "Price Books", icon: BookOpen },
  { id: "payments", label: "Payments", icon: CreditCard },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function CommercePage() {
  const [activeTab, setActiveTab] = useState<TabId>("customers");

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
        <div className="flex items-center gap-3">
          <DollarSign className="w-6 h-6 text-gray-700" />
          <h1 className="text-xl font-semibold text-gray-900">Billing & Commerce</h1>
        </div>
      </div>

      {/* Tab Bar */}
      <div className="flex border-b border-gray-200 px-6">
        {TABS.map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition-colors ${
                isActive
                  ? "border-gray-900 text-gray-900"
                  : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
              }`}
            >
              <Icon className="w-4 h-4" />
              {tab.label}
            </button>
          );
        })}
      </div>

      {/* Tab Content */}
      <div className="flex-1 overflow-auto">
        {activeTab === "customers" && <CustomersListPage />}
        {activeTab === "accounts" && <AccountsListPage />}
        {activeTab === "invoices" && <InvoicesListPage />}
        {activeTab === "price-books" && <PriceBookEditor />}
        {activeTab === "payments" && (
          <div className="p-6">
            <p className="text-gray-500 text-sm">
              Payments are shown on each invoice detail. Use the Invoices tab to view payment history per invoice.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
