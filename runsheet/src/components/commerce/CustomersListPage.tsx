"use client";

import { Gauge, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
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
  type Customer,
  type CustomerFilters,
  type CustomerStatus,
  getCustomers,
} from "../../services/commerceApi";
import LoadingSpinner from "../LoadingSpinner";

// Customer tanks are a property of a customer, reached by clicking the
// customer rather than living as a separate Fuel Ops tab. Lazy-loaded into a
// slide-over.
const CustomerTankPage = lazy(() => import("../ops/CustomerTankPage"));

interface CustomersListPageProps {
  onSelectCustomer?: (customerId: string) => void;
}

export default function CustomersListPage({
  onSelectCustomer,
}: CustomersListPageProps) {
  const router = useRouter();
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<CustomerStatus | "">("");
  // Customer whose tanks are shown in the slide-over (click-through), replacing
  // the former Fuel Ops > Customer Tanks tab.
  const [tanksCustomer, setTanksCustomer] = useState<Customer | null>(null);

  const fetchCustomers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: CustomerFilters = { page, size: 20 };
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
      setError(err instanceof Error ? err.message : "Failed to load customers");
    } finally {
      setLoading(false);
    }
  }, [page, searchQuery, statusFilter]);

  useEffect(() => {
    fetchCustomers();
  }, [fetchCustomers]);

  const getStatusVariant = (
    status: string,
  ): "success" | "warning" | "default" => {
    if (status === "active") return "success";
    if (status === "suspended") return "warning";
    return "default";
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Customers"
        subtitle="Manage customer records and view account projections."
      />

      <FilterBar
        searchPlaceholder="Search by name or email..."
        searchValue={searchQuery}
        onSearchChange={setSearchQuery}
        filters={
          <select
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value as CustomerStatus | "");
              setPage(1);
            }}
            className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
            aria-label="Status"
          >
            <option value="">All</option>
            <option value="active">Active</option>
            <option value="archived">Archived</option>
          </select>
        }
      />

      {/* Error state */}
      {error && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* Loading state */}
      {loading && (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading customers...</span>
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      )}

      {/* Customer table */}
      {!loading &&
        !error &&
        (customers.length === 0 ? (
          <EmptyState
            icon={<span className="text-4xl">👥</span>}
            title="No customers found"
            description="Try adjusting your filters"
          />
        ) : (
          <>
            <Table
              columns={[
                { key: "display_name", label: "Name" },
                {
                  key: "email",
                  label: "Email",
                  render: (customer) => customer.primary_email || "—",
                },
                {
                  key: "status",
                  label: "Status",
                  render: (customer) => (
                    <Badge variant={getStatusVariant(customer.status)}>
                      {customer.status}
                    </Badge>
                  ),
                },
                {
                  key: "account_ids",
                  label: "Accounts",
                  render: (customer) => customer.account_count ?? "—",
                },
                {
                  key: "actions",
                  label: "Actions",
                  render: (customer) => (
                    <div className="flex items-center gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        icon={<Gauge className="w-3.5 h-3.5" />}
                        onClick={() => setTanksCustomer(customer)}
                        aria-label={`View tanks for ${customer.display_name}`}
                      >
                        Tanks
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          if (onSelectCustomer) {
                            onSelectCustomer(customer.customer_id);
                          } else {
                            router.push(
                              `/commerce/customers/${customer.customer_id}`,
                            );
                          }
                        }}
                      >
                        View Details
                      </Button>
                    </div>
                  ),
                },
              ]}
              data={customers}
              keyExtractor={(customer) => customer.customer_id}
            />

            <Pagination
              currentPage={page}
              totalPages={totalPages}
              onPageChange={setPage}
            />
          </>
        ))}

      {/* Customer tanks slide-over — reached by clicking a customer's Tanks
          button (replaces the former Fuel Ops > Customer Tanks tab). */}
      {tanksCustomer && (
        <div
          className="fixed inset-0 z-50 flex justify-end bg-black/30"
          role="dialog"
          aria-modal="true"
          aria-label="Customer tanks"
          onClick={() => setTanksCustomer(null)}
        >
          <div
            className="h-full w-full max-w-4xl overflow-y-auto bg-white shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4">
              <div>
                <h2 className="text-lg font-semibold text-primary">Tanks</h2>
                <p className="text-xs text-gray-500">
                  {tanksCustomer.display_name} · {tanksCustomer.customer_id}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setTanksCustomer(null)}
                className="rounded p-1 text-gray-400 hover:text-gray-600"
                aria-label="Close tanks"
              >
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="p-6">
              <Suspense fallback={<LoadingSpinner message="Loading…" />}>
                <CustomerTankPage
                  customerId={tanksCustomer.customer_id}
                  embedded
                />
              </Suspense>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
