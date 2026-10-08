"use client";

/**
 * Customers list (UI revamp task 3.3): one toolbar (search + status chips
 * with counts), a DataTable, "New customer" in the title row (FormDialog),
 * and a customer's tanks in a Drawer.
 */
import { Eye, Gauge, Plus, RefreshCw } from "lucide-react";
import { useRouter } from "next/navigation";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  Button,
  type Column,
  DataTable,
  Drawer,
  FilterChips,
  IconButton,
  LoadErrorState,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { money, number } from "../../lib/format";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import {
  type Customer,
  type CustomerFilters,
  type CustomerStatus,
  getCustomers,
} from "../../services/commerceApi";
import LoadingSpinner from "../LoadingSpinner";
import { PageTitle } from "../ui/PageHeader";
import CustomerFormDialog from "./CustomerFormDialog";
import { CUSTOMER_STATUS } from "./customerStatus";

// Customer tanks are a property of a customer, reached from the customer's
// row (not a separate Fuel tab). Lazy-loaded into a drawer.
const CustomerTankPage = lazy(() => import("../ops/CustomerTankPage"));
// Customer detail renders in place when no `onSelectCustomer` is wired.
const CustomerDetailPage = lazy(() => import("./CustomerDetailPage"));

const PAGE_SIZE = 20;

const STATUS_CHIPS: { id: "" | CustomerStatus; label: string }[] = [
  { id: "", label: "All" },
  { id: "active", label: "Active" },
  { id: "archived", label: "Archived" },
];

interface CustomersListPageProps {
  onSelectCustomer?: (customerId: string) => void;
}

type Counts = Partial<Record<"" | CustomerStatus, number>>;

function totalOf(response: unknown): number | undefined {
  const r = response as {
    pagination?: { total?: number };
    total?: number;
  };
  return r.pagination?.total ?? r.total;
}

export default function CustomersListPage({
  onSelectCustomer,
}: CustomersListPageProps) {
  const router = useRouter();
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [moduleDisabled, setModuleDisabled] = useState<LoadFailure | null>(
    null,
  );
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<CustomerStatus | "">("");
  const [counts, setCounts] = useState<Counts>({});
  const [creating, setCreating] = useState(false);
  const [reload, setReload] = useState(0);
  const [tanksCustomer, setTanksCustomer] = useState<Customer | null>(null);
  const [selectedCustomerId, setSelectedCustomerId] = useState<string | null>(
    null,
  );

  const fetchCustomers = useCallback(async () => {
    setLoading(true);
    setError(null);
    setModuleDisabled(null);
    try {
      const filters: CustomerFilters = { page, size: PAGE_SIZE };
      if (searchQuery) filters.search = searchQuery;
      if (statusFilter) filters.status = statusFilter;
      const response = await getCustomers(filters);
      setCustomers(response.data ?? []);
      const pagination = (response as { pagination?: { total_pages?: number } })
        .pagination;
      setTotalPages(
        pagination?.total_pages ??
          (response.has_more ? page + 1 : Math.max(page, 1)),
      );
    } catch (err) {
      const failure = classifyLoadError(err, "Failed to load customers");
      if (failure.kind === "module_disabled") setModuleDisabled(failure);
      else setError(failure.message);
    } finally {
      setLoading(false);
    }
    // `reload` forces a refetch after a create.
  }, [page, searchQuery, statusFilter, reload]);

  useEffect(() => {
    fetchCustomers();
  }, [fetchCustomers]);

  // Chip counts: one `size: 1` read per status (no aggregate endpoint),
  // scoped to the current search. A failed count leaves the chip without one.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const next: Counts = {};
      await Promise.allSettled(
        STATUS_CHIPS.map(async (c) => {
          const f: CustomerFilters = { page: 1, size: 1 };
          if (searchQuery) f.search = searchQuery;
          if (c.id) f.status = c.id;
          next[c.id] = totalOf(await getCustomers(f));
        }),
      );
      if (!cancelled) setCounts(next);
    })();
    return () => {
      cancelled = true;
    };
  }, [searchQuery, reload]);

  const openCustomer = useCallback(
    (c: Customer) => {
      if (onSelectCustomer) onSelectCustomer(c.customer_id);
      else setSelectedCustomerId(c.customer_id);
    },
    [onSelectCustomer],
  );

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setCreating(true)}
      >
        New customer
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions: moduleDisabled ? null : actions });

  const columns: Column<Customer>[] = [
    {
      key: "display_name",
      header: "Name",
      truncate: true,
      title: (c) => c.display_name,
      className: "font-medium text-text",
      cell: (c) => c.display_name,
    },
    {
      key: "email",
      header: "Email",
      truncate: true,
      title: (c) => c.primary_email ?? undefined,
      className: "text-slate-700",
      cell: (c) => c.primary_email || "—",
    },
    {
      key: "status",
      header: "Status",
      width: 120,
      cell: (c) => {
        const cfg = CUSTOMER_STATUS[c.status] ?? CUSTOMER_STATUS.active;
        return <StatusBadge status={cfg.status} label={cfg.label} />;
      },
    },
    {
      key: "account_count",
      header: "Accounts",
      align: "right",
      width: 100,
      className: "tabular-nums text-slate-700",
      cell: (c) => number(c.account_count),
    },
    {
      key: "open_balance_cents",
      header: "Open balance",
      align: "right",
      width: 140,
      className: "tabular-nums text-slate-700",
      cell: (c) =>
        c.open_balance_cents == null ? "—" : money(c.open_balance_cents / 100),
    },
  ];

  if (selectedCustomerId) {
    return (
      <Suspense fallback={<LoadingSpinner message="Loading…" />}>
        <CustomerDetailPage
          customerId={selectedCustomerId}
          onBack={() => setSelectedCustomerId(null)}
        />
      </Suspense>
    );
  }

  const titleRow = embedded ? null : (
    <div className="flex h-11 items-center border-b border-slate-200 px-4">
      <PageTitle className="text-base font-semibold text-text">
        Customers
      </PageTitle>
      {!moduleDisabled && <div className="ml-auto">{actions}</div>}
    </div>
  );

  if (moduleDisabled) {
    return (
      <div className="flex h-full flex-col">
        {titleRow}
        <div className="p-4">
          <LoadErrorState
            failure={moduleDisabled}
            entityLabel="Customers"
            onBack={() => router.push("/dashboard")}
            backLabel="Back to Today"
            embedded
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-surface">
      {titleRow}
      <Toolbar
        label="Customers"
        search={
          <input
            type="search"
            value={searchQuery}
            onChange={(e) => {
              setSearchQuery(e.target.value);
              setPage(1);
            }}
            placeholder="Search name or email"
            aria-label="Search"
            className="h-7 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          />
        }
        filters={
          <FilterChips
            label="Customer status"
            options={STATUS_CHIPS.map((c) => ({
              id: c.id || "all",
              label: c.label,
              count: counts[c.id],
              status: c.id ? CUSTOMER_STATUS[c.id].status : undefined,
            }))}
            value={statusFilter || "all"}
            onChange={(v) => {
              setStatusFilter(v === "all" ? "" : (v as CustomerStatus));
              setPage(1);
            }}
          />
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
        <DataTable<Customer>
          ariaLabel="Customers"
          columns={columns}
          data={loading || error ? [] : customers}
          loading={loading}
          error={error ? { message: error, onRetry: fetchCustomers } : null}
          getRowId={(c) => c.customer_id}
          rowLabel={(c) => c.display_name}
          onRowClick={openCustomer}
          rowMenu={(c) => [
            {
              id: "details",
              label: "View details",
              icon: <Eye className="h-3.5 w-3.5" />,
              onSelect: () => openCustomer(c),
            },
            {
              id: "tanks",
              label: "View tanks",
              icon: <Gauge className="h-3.5 w-3.5" />,
              onSelect: () => setTanksCustomer(c),
            },
          ]}
          pagination={
            totalPages > 1
              ? { page, totalPages, onPageChange: setPage }
              : undefined
          }
          emptyState={
            <div className="text-text-muted">
              <p className="text-sm font-medium">No customers found</p>
              <p className="mt-1 text-xs">Try adjusting your filters</p>
            </div>
          }
        />
      </div>

      <Drawer
        open={tanksCustomer !== null}
        onClose={() => setTanksCustomer(null)}
        title={
          tanksCustomer ? `Tanks · ${tanksCustomer.display_name}` : "Tanks"
        }
        width={880}
      >
        {tanksCustomer && (
          <Suspense fallback={<LoadingSpinner message="Loading…" />}>
            <CustomerTankPage customerId={tanksCustomer.customer_id} embedded />
          </Suspense>
        )}
      </Drawer>

      <CustomerFormDialog
        open={creating}
        onClose={() => setCreating(false)}
        onSaved={() => {
          setPage(1);
          setReload((n) => n + 1);
        }}
      />
    </div>
  );
}
