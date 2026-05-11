"use client";

import { useCallback, useEffect, useState } from "react";
import { Badge, Button, EmptyState, StatsBar, Table } from "@/components/ui";
import {
  type Account,
  type CustomerWithProjections,
  getAccounts,
  getCustomer,
  getInvoices,
  type Invoice,
} from "../../services/commerceApi";

interface CustomerDetailPageProps {
  customerId: string;
  onBack?: () => void;
  onViewFuelOrders?: (customerId: string) => void;
  onViewAccount?: (accountId: string) => void;
}

export default function CustomerDetailPage({
  customerId,
  onBack,
  onViewFuelOrders,
  onViewAccount,
}: CustomerDetailPageProps) {
  const [customer, setCustomer] = useState<CustomerWithProjections | null>(
    null,
  );
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [recentInvoices, setRecentInvoices] = useState<Invoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [customerRes, accountsRes, invoicesRes] = await Promise.all([
        getCustomer(customerId),
        getAccounts({ customer_id: customerId, size: 50 }),
        getInvoices({ customer_id: customerId, size: 5 }),
      ]);
      setCustomer(customerRes.data);
      setAccounts(accountsRes.data);
      setRecentInvoices(invoicesRes.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load customer details",
      );
    } finally {
      setLoading(false);
    }
  }, [customerId]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12">
        <span className="sr-only">Loading customer details...</span>
        <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div role="alert" className="p-6">
        <div className="bg-error-light border border-error-light text-error-dark p-4 rounded">
          {error}
        </div>
      </div>
    );
  }

  if (!customer) return null;

  const totalOpenBalance = accounts.reduce(
    (sum, a) => sum + a.open_balance_cents,
    0,
  );

  return (
    <div className="p-6">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center gap-4 mb-2">
          {onBack && (
            <Button variant="ghost" size="sm" onClick={onBack}>
              ← Back to Customers
            </Button>
          )}
        </div>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">{customer.display_name}</h1>
            <p className="text-gray-600">
              {customer.legal_name && `${customer.legal_name} · `}
              {customer.primary_email || "No email"}
            </p>
          </div>
          <Badge variant={customer.status === "active" ? "success" : "default"}>
            {customer.status}
          </Badge>
        </div>
      </div>

      {/* Summary */}
      <section aria-labelledby="projections-heading" className="mb-8">
        <h2 id="projections-heading" className="text-lg font-semibold mb-3">
          Summary
        </h2>
        <StatsBar
          stats={[
            { label: "Accounts", value: accounts.length.toString() },
            {
              label: "Open Balance",
              value: `$${(totalOpenBalance / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`,
            },
            {
              label: "Recent Invoices",
              value: recentInvoices.length.toString(),
            },
          ]}
        />
      </section>

      {/* Cross-link to fuel order history */}
      <section aria-labelledby="fuel-orders-heading" className="mb-8">
        <h2 id="fuel-orders-heading" className="text-lg font-semibold mb-3">
          Fuel Order History
        </h2>
        <Button
          variant="secondary"
          onClick={() => onViewFuelOrders?.(customerId)}
        >
          View Fuel Orders for this Customer →
        </Button>
      </section>

      {/* Accounts list */}
      <section aria-labelledby="accounts-heading" className="mb-8">
        <h2 id="accounts-heading" className="text-lg font-semibold mb-3">
          Accounts
        </h2>
        {accounts.length === 0 ? (
          <EmptyState
            icon={<span className="text-4xl">📊</span>}
            title="No accounts"
            description="No accounts linked to this customer."
          />
        ) : (
          <Table
            columns={[
              { key: "display_name", label: "Account" },
              {
                key: "tier",
                label: "Tier",
                render: (account) => (
                  <span className="capitalize">{account.tier}</span>
                ),
              },
              {
                key: "credit_state",
                label: "Credit State",
                render: (account) => (
                  <Badge
                    variant={
                      account.credit_state === "ok"
                        ? "success"
                        : account.credit_state === "hold"
                          ? "error"
                          : "warning"
                    }
                  >
                    {account.credit_state.replace(/_/g, " ")}
                  </Badge>
                ),
              },
              {
                key: "open_balance_cents",
                label: "Open Balance",
                render: (account) =>
                  `$${(account.open_balance_cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`,
              },
              {
                key: "actions",
                label: "Actions",
                render: (account) => (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onViewAccount?.(account.account_id)}
                  >
                    View
                  </Button>
                ),
              },
            ]}
            data={accounts}
            getRowId={(account) => account.account_id}
          />
        )}
      </section>

      {/* Recent invoices */}
      <section aria-labelledby="recent-invoices-heading">
        <h2 id="recent-invoices-heading" className="text-lg font-semibold mb-3">
          Recent Invoices
        </h2>
        {recentInvoices.length === 0 ? (
          <EmptyState
            icon={<span className="text-4xl">📄</span>}
            title="No invoices"
            description="No invoices yet."
          />
        ) : (
          <Table
            columns={[
              { key: "invoice_number", label: "Invoice #" },
              {
                key: "status",
                label: "Status",
                render: (inv) => (
                  <span className="capitalize">{inv.status}</span>
                ),
              },
              {
                key: "total_cents",
                label: "Total",
                render: (inv) =>
                  `$${(inv.total_cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`,
              },
              { key: "due_date", label: "Due Date" },
            ]}
            data={recentInvoices}
            getRowId={(inv) => inv.invoice_id}
          />
        )}
      </section>
    </div>
  );
}
