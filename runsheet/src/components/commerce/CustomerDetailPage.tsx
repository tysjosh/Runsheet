"use client";

import React, { useCallback, useEffect, useState } from "react";
import type { Account, Customer, Invoice } from "../../types/commerce";
import { getCustomer, getAccounts, getInvoices } from "../../services/commerceApi";

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
  const [customer, setCustomer] = useState<Customer | null>(null);
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
        <div className="animate-spin h-8 w-8 border-4 border-blue-600 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div role="alert" className="p-6">
        <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded">
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
      <header className="mb-6">
        <div className="flex items-center gap-4 mb-2">
          {onBack && (
            <button
              type="button"
              onClick={onBack}
              className="text-blue-600 hover:underline"
            >
              ← Back to Customers
            </button>
          )}
        </div>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">{customer.display_name}</h1>
            <p className="text-gray-600">
              {customer.legal_name && `${customer.legal_name} · `}
              {customer.email || "No email"}
            </p>
          </div>
          <span
            className={`px-3 py-1 rounded text-sm font-medium ${
              customer.status === "active"
                ? "bg-green-100 text-green-800"
                : customer.status === "suspended"
                  ? "bg-yellow-100 text-yellow-800"
                  : "bg-gray-100 text-gray-800"
            }`}
          >
            {customer.status}
          </span>
        </div>
      </header>

      {/* Projections summary */}
      <section aria-labelledby="projections-heading" className="mb-8">
        <h2 id="projections-heading" className="text-lg font-semibold mb-3">
          Summary
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Accounts</p>
            <p className="text-2xl font-bold">{accounts.length}</p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Open Balance</p>
            <p className="text-2xl font-bold">
              ${(totalOpenBalance / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}
            </p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Recent Invoices</p>
            <p className="text-2xl font-bold">{recentInvoices.length}</p>
          </div>
        </div>
      </section>

      {/* Cross-link to fuel order history */}
      <section aria-labelledby="fuel-orders-heading" className="mb-8">
        <h2 id="fuel-orders-heading" className="text-lg font-semibold mb-3">
          Fuel Order History
        </h2>
        <button
          type="button"
          onClick={() => onViewFuelOrders?.(customerId)}
          className="bg-gray-100 hover:bg-gray-200 border rounded px-4 py-2 text-sm"
        >
          View Fuel Orders for this Customer →
        </button>
      </section>

      {/* Accounts list */}
      <section aria-labelledby="accounts-heading" className="mb-8">
        <h2 id="accounts-heading" className="text-lg font-semibold mb-3">
          Accounts
        </h2>
        {accounts.length === 0 ? (
          <p className="text-gray-500">No accounts linked to this customer.</p>
        ) : (
          <table className="w-full border-collapse" role="table">
            <thead>
              <tr className="border-b bg-gray-50">
                <th className="text-left p-3 font-medium">Account</th>
                <th className="text-left p-3 font-medium">Tier</th>
                <th className="text-left p-3 font-medium">Credit State</th>
                <th className="text-left p-3 font-medium">Open Balance</th>
                <th className="text-left p-3 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((account) => (
                <tr key={account.account_id} className="border-b hover:bg-gray-50">
                  <td className="p-3">{account.display_name}</td>
                  <td className="p-3 capitalize">{account.tier}</td>
                  <td className="p-3">
                    <span
                      className={`inline-block px-2 py-1 rounded text-xs font-medium ${
                        account.credit_state === "good_standing"
                          ? "bg-green-100 text-green-800"
                          : account.credit_state === "on_hold"
                            ? "bg-red-100 text-red-800"
                            : "bg-yellow-100 text-yellow-800"
                      }`}
                    >
                      {account.credit_state.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="p-3">
                    ${(account.open_balance_cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </td>
                  <td className="p-3">
                    <button
                      type="button"
                      onClick={() => onViewAccount?.(account.account_id)}
                      className="text-blue-600 hover:underline"
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* Recent invoices */}
      <section aria-labelledby="recent-invoices-heading">
        <h2 id="recent-invoices-heading" className="text-lg font-semibold mb-3">
          Recent Invoices
        </h2>
        {recentInvoices.length === 0 ? (
          <p className="text-gray-500">No invoices yet.</p>
        ) : (
          <table className="w-full border-collapse" role="table">
            <thead>
              <tr className="border-b bg-gray-50">
                <th className="text-left p-3 font-medium">Invoice #</th>
                <th className="text-left p-3 font-medium">Status</th>
                <th className="text-left p-3 font-medium">Total</th>
                <th className="text-left p-3 font-medium">Due Date</th>
              </tr>
            </thead>
            <tbody>
              {recentInvoices.map((inv) => (
                <tr key={inv.invoice_id} className="border-b">
                  <td className="p-3">{inv.invoice_number}</td>
                  <td className="p-3 capitalize">{inv.status}</td>
                  <td className="p-3">
                    ${(inv.total_cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </td>
                  <td className="p-3">{inv.due_date}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
