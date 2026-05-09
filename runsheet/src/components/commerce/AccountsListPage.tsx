"use client";

import React, { useCallback, useEffect, useState } from "react";
import type { Account, PaginatedResponse } from "../../types/commerce";
import { getAccounts, type AccountFilters } from "../../services/commerceApi";

interface AccountsListPageProps {
  onSelectAccount?: (accountId: string) => void;
}

export default function AccountsListPage({
  onSelectAccount,
}: AccountsListPageProps) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [tierFilter, setTierFilter] = useState<string>("");
  const [creditStateFilter, setCreditStateFilter] = useState<string>("");

  const fetchAccounts = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: AccountFilters = { page, size: 20 };
      if (statusFilter) filters.status = statusFilter;
      if (tierFilter) filters.tier = tierFilter;
      if (creditStateFilter) filters.credit_state = creditStateFilter;

      const response: PaginatedResponse<Account> = await getAccounts(filters);
      setAccounts(response.data);
      setTotalPages(response.pagination.total_pages);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load accounts",
      );
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter, tierFilter, creditStateFilter]);

  useEffect(() => {
    fetchAccounts();
  }, [fetchAccounts]);

  return (
    <div className="p-6">
      <header className="mb-6">
        <h1 className="text-2xl font-bold">Accounts</h1>
        <p className="text-gray-600 mt-1">
          Manage billing accounts, credit states, and aging.
        </p>
      </header>

      {/* Filters */}
      <div className="flex gap-4 mb-6 flex-wrap">
        <div>
          <label htmlFor="account-status-filter" className="block text-sm font-medium mb-1">
            Status
          </label>
          <select
            id="account-status-filter"
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value);
              setPage(1);
            }}
            className="border rounded px-3 py-2"
          >
            <option value="">All</option>
            <option value="active">Active</option>
            <option value="suspended">Suspended</option>
            <option value="closed">Closed</option>
          </select>
        </div>
        <div>
          <label htmlFor="tier-filter" className="block text-sm font-medium mb-1">
            Tier
          </label>
          <select
            id="tier-filter"
            value={tierFilter}
            onChange={(e) => {
              setTierFilter(e.target.value);
              setPage(1);
            }}
            className="border rounded px-3 py-2"
          >
            <option value="">All</option>
            <option value="default">Default</option>
            <option value="preferred">Preferred</option>
            <option value="enterprise">Enterprise</option>
          </select>
        </div>
        <div>
          <label htmlFor="credit-state-filter" className="block text-sm font-medium mb-1">
            Credit State
          </label>
          <select
            id="credit-state-filter"
            value={creditStateFilter}
            onChange={(e) => {
              setCreditStateFilter(e.target.value);
              setPage(1);
            }}
            className="border rounded px-3 py-2"
          >
            <option value="">All</option>
            <option value="good_standing">Good Standing</option>
            <option value="on_hold">On Hold</option>
            <option value="override_active">Override Active</option>
            <option value="suspended">Suspended</option>
          </select>
        </div>
      </div>

      {/* Error state */}
      {error && (
        <div role="alert" className="bg-red-50 border border-red-200 text-red-700 p-4 rounded mb-4">
          {error}
        </div>
      )}

      {/* Loading state */}
      {loading && (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading accounts...</span>
          <div className="animate-spin h-8 w-8 border-4 border-blue-600 border-t-transparent rounded-full" />
        </div>
      )}

      {/* Accounts table */}
      {!loading && !error && (
        <>
          <table className="w-full border-collapse" role="table">
            <thead>
              <tr className="border-b bg-gray-50">
                <th className="text-left p-3 font-medium">Account</th>
                <th className="text-left p-3 font-medium">Tier</th>
                <th className="text-left p-3 font-medium">Credit State</th>
                <th className="text-left p-3 font-medium">Credit Limit</th>
                <th className="text-left p-3 font-medium">Open Balance</th>
                <th className="text-left p-3 font-medium">Net Terms</th>
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
                            : account.credit_state === "override_active"
                              ? "bg-blue-100 text-blue-800"
                              : "bg-yellow-100 text-yellow-800"
                      }`}
                    >
                      {account.credit_state.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="p-3">
                    ${(account.credit_limit_cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </td>
                  <td className="p-3">
                    ${(account.open_balance_cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </td>
                  <td className="p-3">{account.net_terms_days} days</td>
                  <td className="p-3">
                    <button
                      type="button"
                      onClick={() => onSelectAccount?.(account.account_id)}
                      className="text-blue-600 hover:underline"
                    >
                      View Details
                    </button>
                  </td>
                </tr>
              ))}
              {accounts.length === 0 && (
                <tr>
                  <td colSpan={7} className="p-6 text-center text-gray-500">
                    No accounts found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          {/* Pagination */}
          <nav aria-label="Pagination" className="flex justify-between items-center mt-4">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
              className="px-3 py-1 border rounded disabled:opacity-50"
            >
              Previous
            </button>
            <span className="text-sm text-gray-600">
              Page {page} of {totalPages}
            </span>
            <button
              type="button"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
              className="px-3 py-1 border rounded disabled:opacity-50"
            >
              Next
            </button>
          </nav>
        </>
      )}
    </div>
  );
}
