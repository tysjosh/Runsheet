"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  FilterBar,
  PageHeader,
  Pagination,
  Table,
} from "@/components/ui";
import {
  type Account,
  type AccountFilters,
  type AccountStatus,
  type AccountTier,
  type CreditState,
  getAccounts,
} from "../../services/commerceApi";

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
  const [statusFilter, setStatusFilter] = useState<AccountStatus | "">("");
  const [tierFilter, setTierFilter] = useState<AccountTier | "">("");
  const [creditStateFilter, setCreditStateFilter] = useState<CreditState | "">(
    "",
  );

  const fetchAccounts = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: AccountFilters = { page, size: 20 };
      if (statusFilter) filters.status = statusFilter;
      if (tierFilter) filters.tier = tierFilter;
      if (creditStateFilter) filters.credit_state = creditStateFilter;

      const response = await getAccounts(filters);
      setAccounts(response.data ?? []);
      const pagination = (response as { pagination?: { total_pages?: number } })
        .pagination;
      setTotalPages(
        pagination?.total_pages ??
          (response.has_more ? page + 1 : Math.max(page, 1)),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load accounts");
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter, tierFilter, creditStateFilter]);

  useEffect(() => {
    fetchAccounts();
  }, [fetchAccounts]);

  const getCreditStateBadgeVariant = (state: string) => {
    if (state === "ok") return "success";
    if (state === "hold") return "error";
    if (state === "override") return "info";
    return "warning";
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Accounts"
        subtitle="Manage billing accounts, credit states, and aging."
      />

      <FilterBar
        filters={
          <>
            <select
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value as AccountStatus | "");
                setPage(1);
              }}
              className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
              aria-label="Status"
            >
              <option value="">All</option>
              <option value="active">Active</option>
              <option value="suspended">Suspended</option>
              <option value="closed">Closed</option>
            </select>
            <select
              value={tierFilter}
              onChange={(e) => {
                setTierFilter(e.target.value as AccountTier | "");
                setPage(1);
              }}
              className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
              aria-label="Tier"
            >
              <option value="">All</option>
              <option value="default">Default</option>
              <option value="platinum">Platinum</option>
              <option value="gold">Gold</option>
              <option value="silver">Silver</option>
              <option value="bronze">Bronze</option>
            </select>
            <select
              value={creditStateFilter}
              onChange={(e) => {
                setCreditStateFilter(e.target.value as CreditState | "");
                setPage(1);
              }}
              className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
              aria-label="Credit State"
            >
              <option value="">All</option>
              <option value="ok">OK</option>
              <option value="hold">Hold</option>
              <option value="override">Override</option>
            </select>
          </>
        }
      />

      {error && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {loading && (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading accounts...</span>
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      )}

      {!loading &&
        !error &&
        (accounts.length === 0 ? (
          <EmptyState
            icon={<span className="text-4xl">📊</span>}
            title="No accounts found"
            description="Try adjusting your filters"
          />
        ) : (
          <>
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
                      variant={getCreditStateBadgeVariant(account.credit_state)}
                    >
                      {account.credit_state.replace(/_/g, " ")}
                    </Badge>
                  ),
                },
                {
                  key: "credit_limit_cents",
                  label: "Credit Limit",
                  render: (account) =>
                    `$${(account.credit_limit_cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`,
                },
                {
                  key: "open_balance_cents",
                  label: "Open Balance",
                  render: (account) =>
                    `$${(account.open_balance_cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`,
                },
                {
                  key: "net_terms_days",
                  label: "Net Terms",
                  render: (account) => `${account.net_terms_days} days`,
                },
                {
                  key: "actions",
                  label: "Actions",
                  render: (account) => (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onSelectAccount?.(account.account_id)}
                    >
                      View Details
                    </Button>
                  ),
                },
              ]}
              data={accounts}
              keyExtractor={(account) => account.account_id}
            />

            <Pagination
              currentPage={page}
              totalPages={totalPages}
              onPageChange={setPage}
            />
          </>
        ))}
    </div>
  );
}
