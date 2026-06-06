"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Badge, Button } from "@/components/ui";
import {
  type CustomerWithProjections,
  getCustomer,
} from "../../services/commerceApi";

interface CustomerDetailPageProps {
  customerId: string;
}

export default function CustomerDetailPage({
  customerId,
}: CustomerDetailPageProps) {
  const router = useRouter();
  const [customer, setCustomer] = useState<CustomerWithProjections | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchCustomer = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getCustomer(customerId);
      setCustomer(response.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load customer details",
      );
    } finally {
      setLoading(false);
    }
  }, [customerId]);

  useEffect(() => {
    fetchCustomer();
  }, [fetchCustomer]);

  const getStatusVariant = (
    status: string,
  ): "success" | "warning" | "default" => {
    if (status === "active") return "success";
    if (status === "suspended") return "warning";
    return "default";
  };

  const formatCents = (cents: number) =>
    `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  };

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

  return (
    <div className="p-6">
      {/* Header */}
      <header className="mb-6">
        <div className="flex items-center gap-4 mb-2">
          <Button
            variant="ghost"
            onClick={() => router.push("/commerce/customers")}
          >
            ← Back to Customers
          </Button>
        </div>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">{customer.display_name}</h1>
            {customer.legal_name && (
              <p className="text-gray-600">{customer.legal_name}</p>
            )}
          </div>
          <Badge variant={getStatusVariant(customer.status)}>
            {customer.status}
          </Badge>
        </div>
      </header>

      {/* Summary cards */}
      <section aria-labelledby="summary-heading" className="mb-8">
        <h2 id="summary-heading" className="text-lg font-semibold mb-3">
          Customer Summary
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Accounts</p>
            <p className="text-2xl font-bold">{customer.account_count}</p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Open Invoices</p>
            <p className="text-2xl font-bold">{customer.open_invoice_count}</p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Open Balance</p>
            <p className="text-2xl font-bold">
              {formatCents(customer.open_balance_cents)}
            </p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Lifetime Revenue</p>
            <p className="text-2xl font-bold">
              {formatCents(customer.lifetime_revenue_cents)}
            </p>
          </div>
        </div>
      </section>

      {/* Customer Information */}
      <section aria-labelledby="info-heading" className="mb-8">
        <h2 id="info-heading" className="text-lg font-semibold mb-3">
          Customer Information
        </h2>
        <div className="border rounded p-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div>
              <p className="text-sm text-gray-600 mb-1">Display Name</p>
              <p className="font-medium">{customer.display_name}</p>
            </div>

            {customer.legal_name && (
              <div>
                <p className="text-sm text-gray-600 mb-1">Legal Name</p>
                <p className="font-medium">{customer.legal_name}</p>
              </div>
            )}

            {customer.primary_email && (
              <div>
                <p className="text-sm text-gray-600 mb-1">Primary Email</p>
                <p className="font-medium">{customer.primary_email}</p>
              </div>
            )}

            {customer.tax_id && (
              <div>
                <p className="text-sm text-gray-600 mb-1">Tax ID</p>
                <p className="font-medium">{customer.tax_id}</p>
              </div>
            )}

            <div>
              <p className="text-sm text-gray-600 mb-1">Customer ID</p>
              <p className="font-mono text-sm">{customer.customer_id}</p>
            </div>

            <div>
              <p className="text-sm text-gray-600 mb-1">Status</p>
              <Badge variant={getStatusVariant(customer.status)}>
                {customer.status}
              </Badge>
            </div>

            <div>
              <p className="text-sm text-gray-600 mb-1">Created</p>
              <p className="font-medium">{formatDate(customer.created_at)}</p>
            </div>

            <div>
              <p className="text-sm text-gray-600 mb-1">Last Updated</p>
              <p className="font-medium">{formatDate(customer.updated_at)}</p>
            </div>
          </div>
        </div>
      </section>

      {/* Related Records */}
      <section aria-labelledby="related-heading" className="mb-8">
        <h2 id="related-heading" className="text-lg font-semibold mb-3">
          Related Records
        </h2>
        <div className="border rounded p-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div>
              <p className="text-sm text-gray-600 mb-1">Accounts</p>
              <p className="text-2xl font-bold">{customer.account_count}</p>
              <p className="text-xs text-gray-500 mt-1">
                Total accounts for this customer
              </p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Open Invoices</p>
              <p className="text-2xl font-bold">
                {customer.open_invoice_count}
              </p>
              <p className="text-xs text-gray-500 mt-1">
                Invoices pending payment
              </p>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
