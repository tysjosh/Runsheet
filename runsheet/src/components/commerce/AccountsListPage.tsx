"use client";

/**
 * Billing → Accounts (UI revamp task 3.4): one toolbar (status chips with
 * counts, a Filters popover for tier and credit state), a DataTable, and
 * money and terms through `lib/format`.
 */
import { Eye, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  type Column,
  DataTable,
  Field,
  FilterChips,
  FilterPopover,
  IconButton,
  Select,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { humanize, money, number } from "../../lib/format";
import {
  type Account,
  type AccountFilters,
  type AccountStatus,
  type AccountTier,
  type CreditState,
  getAccounts,
} from "../../services/commerceApi";
import { PageTitle } from "../ui/PageHeader";
import {
  ACCOUNT_STATUS,
  AccountStatusBadge,
  CREDIT_STATE,
  CreditStateBadge,
} from "./billingStatus";

const PAGE_SIZE = 20;

const STATUS_CHIPS: { id: "" | AccountStatus; label: string }[] = [
  { id: "", label: "All" },
  { id: "active", label: "Active" },
  { id: "suspended", label: "Suspended" },
  { id: "closed", label: "Closed" },
];

const TIERS: { value: "" | AccountTier; label: string }[] = [
  { value: "", label: "Any tier" },
  { value: "default", label: "Default" },
  { value: "platinum", label: "Platinum" },
  { value: "gold", label: "Gold" },
  { value: "silver", label: "Silver" },
  { value: "bronze", label: "Bronze" },
];

const CREDIT_STATES: { value: "" | CreditState; label: string }[] = [
  { value: "", label: "Any credit state" },
  ...(Object.keys(CREDIT_STATE) as CreditState[]).map((k) => ({
    value: k,
    label: CREDIT_STATE[k].label,
  })),
];

type Counts = Partial<Record<"" | AccountStatus, number>>;

function totalOf(response: unknown): number | undefined {
  const r = response as { pagination?: { total?: number }; total?: number };
  return r.pagination?.total ?? r.total;
}

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
  const [counts, setCounts] = useState<Counts>({});
  const [reload, setReload] = useState(0);

  const fetchAccounts = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: AccountFilters = { page, size: PAGE_SIZE };
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
    // `reload` forces a refetch from the Refresh button.
  }, [page, statusFilter, tierFilter, creditStateFilter, reload]);

  useEffect(() => {
    fetchAccounts();
  }, [fetchAccounts]);

  // Chip counts: one `size: 1` read per status within the popover filters
  // (no aggregate endpoint). A failed or total-less read leaves no count.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const next: Counts = {};
      await Promise.allSettled(
        STATUS_CHIPS.map(async (c) => {
          const f: AccountFilters = { page: 1, size: 1 };
          if (c.id) f.status = c.id;
          if (tierFilter) f.tier = tierFilter;
          if (creditStateFilter) f.credit_state = creditStateFilter;
          next[c.id] = totalOf(await getAccounts(f));
        }),
      );
      if (!cancelled) setCounts(next);
    })();
    return () => {
      cancelled = true;
    };
  }, [tierFilter, creditStateFilter, reload]);

  const embedded = usePageChrome({});
  const open = (a: Account) => onSelectAccount?.(a.account_id);

  const columns: Column<Account>[] = [
    {
      key: "display_name",
      header: "Account",
      truncate: true,
      title: (a) => a.display_name,
      className: "font-medium text-text",
      cell: (a) => a.display_name,
    },
    {
      key: "status",
      header: "Status",
      width: 130,
      cell: (a) => <AccountStatusBadge status={a.status} />,
    },
    {
      key: "tier",
      header: "Tier",
      width: 110,
      className: "text-slate-700",
      cell: (a) => humanize(a.tier),
    },
    {
      key: "credit_state",
      header: "Credit",
      width: 120,
      cell: (a) => <CreditStateBadge state={a.credit_state} />,
    },
    {
      key: "credit_limit_cents",
      header: "Credit limit",
      align: "right",
      width: 140,
      className: "tabular-nums text-slate-700",
      cell: (a) => money(a.credit_limit_cents / 100),
    },
    {
      key: "open_balance_cents",
      header: "Open balance",
      align: "right",
      width: 140,
      className: "tabular-nums text-text",
      cell: (a) => money(a.open_balance_cents / 100),
    },
    {
      key: "net_terms_days",
      header: "Terms",
      align: "right",
      width: 100,
      className: "tabular-nums text-slate-700",
      cell: (a) => `Net ${number(a.net_terms_days)}`,
    },
  ];

  const popoverCount = (tierFilter ? 1 : 0) + (creditStateFilter ? 1 : 0);

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Accounts
          </PageTitle>
        </div>
      )}
      <Toolbar
        label="Accounts"
        filters={
          <>
            <FilterChips
              label="Account status"
              options={STATUS_CHIPS.map((c) => ({
                id: c.id || "all",
                label: c.label,
                count: counts[c.id],
                status: c.id ? ACCOUNT_STATUS[c.id].status : undefined,
              }))}
              value={statusFilter || "all"}
              onChange={(v) => {
                setStatusFilter(v === "all" ? "" : (v as AccountStatus));
                setPage(1);
              }}
            />
            <FilterPopover
              count={popoverCount}
              label="Account filters"
              onClear={() => {
                setTierFilter("");
                setCreditStateFilter("");
                setPage(1);
              }}
            >
              <Field label="Tier" id="accounts-filter-tier">
                <Select
                  id="accounts-filter-tier"
                  value={tierFilter}
                  onChange={(v) => {
                    setTierFilter(v as AccountTier | "");
                    setPage(1);
                  }}
                  options={TIERS}
                />
              </Field>
              <Field label="Credit state" id="accounts-filter-credit">
                <Select
                  id="accounts-filter-credit"
                  value={creditStateFilter}
                  onChange={(v) => {
                    setCreditStateFilter(v as CreditState | "");
                    setPage(1);
                  }}
                  options={CREDIT_STATES}
                />
              </Field>
            </FilterPopover>
          </>
        }
        end={
          <IconButton
            label="Refresh"
            size="sm"
            onClick={() => setReload((n) => n + 1)}
            icon={
              <RefreshCw
                className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
              />
            }
          />
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<Account>
          ariaLabel="Accounts"
          columns={columns}
          data={loading || error ? [] : accounts}
          loading={loading}
          error={error ? { message: error, onRetry: fetchAccounts } : null}
          getRowId={(a) => a.account_id}
          rowLabel={(a) => a.display_name}
          onRowClick={onSelectAccount ? open : undefined}
          rowMenu={
            onSelectAccount
              ? (a) => [
                  {
                    id: "view",
                    label: "View account",
                    icon: <Eye className="h-3.5 w-3.5" />,
                    onSelect: () => open(a),
                  },
                ]
              : undefined
          }
          pagination={
            totalPages > 1
              ? { page, totalPages, onPageChange: setPage }
              : undefined
          }
          emptyState={
            <div className="text-text-muted">
              <p className="text-sm font-medium">No accounts found</p>
              <p className="mt-1 text-xs">Try adjusting your filters</p>
            </div>
          }
        />
      </div>
    </div>
  );
}
