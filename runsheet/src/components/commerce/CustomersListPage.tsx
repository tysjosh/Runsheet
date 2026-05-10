"use client";

import React, { useCallback, useEffect, useState } from "react";
import type { Customer, PaginatedResponse } from "../../types/commerce";
import { getCustomers, type CustomerFilters } from "../../services/commerceApi";

interface CustomersListPageProps {
  onSelectCustomer?: (customerId: string) => void;
}

export default function CustomersListPage({
  onSelectCustomer,
}: CustomersListPageProps) {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("");

  const fetchCustomers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: CustomerFilters = { page, size: 20 };
      if (searchQuery) filters.search = searchQuery;
      if (statusFilter) filters.status = statusFilter;

      const response: PaginatedResponse<Customer> =
        await getCustomers(filters);
      setCustomers(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load customers",
      );
    } finally {
      setLoading(false);
    }
  }, [page, searchQuery, statusFilter]);

  useEffect(() => {
    fetchCustomers();
  }, [fetchCustomers]);

  const handleSearch = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setPage(1);
    fetchCustomers();
  };

  return (
    <div className="p-6">
      <header className="mb-6">
        <h1 className="text-2xl font-bold">Customers</h1>
        <p className="text-gray-600 mt-1">
          Manage customer records and view account projections.
        </p>
      </header>

      {/* Filters */}
      <form
        onSubmit={handleSearch}
        className="flex gap-4 mb-6 items-end"
        role="search"
      >
        <div className="flex-1">
          <label htmlFor="customer-search" className="block text-sm font-medium mb-1">
            Search
          </label>
          <input
            id="customer-search"
            type="search"
            placeholder="Search by name or email..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
        <div>
          <label htmlFor="status-filter" className="block text-sm font-medium mb-1">
            Status
          </label>
          <select
            id="status-filter"
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value);
              setPage(1);
            }}
            className="border rounded px-3 py-2"
          >
            <option value="">All</option>
            <option value="active">Active</option>
            <option value="archived">Archived</option>
            <option value="suspended">Suspended</option>
          </select>
        </div>
        <button
          type="submit"
          className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700"
        >
          Search
        </button>
      </form>

      {/* Error state */}
      {error && (
        <div role="alert" className="bg-red-50 border border-red-200 text-red-700 p-4 rounded mb-4">
          {error}
        </div>
      )}

      {/* Loading state */}
      {loading && (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading customers...</span>
          <div className="animate-spin h-8 w-8 border-4 border-blue-600 border-t-transparent rounded-full" />
        </div>
      )}

      {/* Customer table */}
      {!loading && !error && (
        <>
          <table className="w-full border-collapse" role="table">
            <thead>
              <tr className="border-b bg-gray-50">
                <th className="text-left p-3 font-medium">Name</th>
                <th className="text-left p-3 font-medium">Email</th>
                <th className="text-left p-3 font-medium">Status</th>
                <th className="text-left p-3 font-medium">Accounts</th>
                <th className="text-left p-3 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {customers.map((customer) => (
                <tr key={customer.customer_id} className="border-b hover:bg-gray-50">
                  <td className="p-3">{customer.display_name}</td>
                  <td className="p-3">{customer.email || "—"}</td>
                  <td className="p-3">
                    <span
                      className={`inline-block px-2 py-1 rounded text-xs font-medium ${
                        customer.status === "active"
                          ? "bg-green-100 text-green-800"
                          : customer.status === "suspended"
                            ? "bg-yellow-100 text-yellow-800"
                            : "bg-gray-100 text-gray-800"
                      }`}
                    >
                      {customer.status}
                    </span>
                  </td>
                  <td className="p-3">{customer.account_ids.length}</td>
                  <td className="p-3">
                    <button
                      type="button"
                      onClick={() => onSelectCustomer?.(customer.customer_id)}
                      className="text-blue-600 hover:underline"
                    >
                      View Details
                    </button>
                  </td>
                </tr>
              ))}
              {customers.length === 0 && (
                <tr>
                  <td colSpan={5} className="p-6 text-center text-gray-500">
                    No customers found.
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
