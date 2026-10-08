"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Badge, Button, LoadErrorState } from "@/components/ui";
import { hasAnyRole } from "../../config/modules";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import {
  type CustomerWithProjections,
  getCustomer,
} from "../../services/commerceApi";
import { getCurrentUserRoles } from "../../utils/auth";
import { PageTitle } from "../ui/PageHeader";
import PortalAccessPanel from "./PortalAccessPanel";

interface CustomerDetailPageProps {
  customerId: string;
  /**
   * Optional in-shell back handler. When provided (e.g. the dashboard shell
   * renders detail in place), the Back button clears the selection instead of
   * route-navigating to /commerce/customers.
   */
  onBack?: () => void;
}

export default function CustomerDetailPage({
  customerId,
  onBack,
}: CustomerDetailPageProps) {
  const router = useRouter();
  const [customer, setCustomer] = useState<CustomerWithProjections | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [loadFailure, setLoadFailure] = useState<LoadFailure | null>(null);

  const fetchCustomer = useCallback(async () => {
    setLoading(true);
    setLoadFailure(null);
    try {
      const response = await getCustomer(customerId);
      setCustomer(response.data);
    } catch (err) {
      setLoadFailure(classifyLoadError(err, "Failed to load customer details"));
    } finally {
      setLoading(false);
    }
  }, [customerId]);

  useEffect(() => {
    fetchCustomer();
  }, [fetchCustomer]);

  // Portal access is admin-only (PD24); the API re-checks the role.
  const [roles, setRoles] = useState<readonly string[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    void getCurrentUserRoles().then((r) => {
      if (!cancelled) setRoles(r ?? []);
    });
    return () => {
      cancelled = true;
    };
  }, []);

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

  if (loadFailure) {
    return (
      <LoadErrorState
        failure={loadFailure}
        entityLabel="Customer"
        entityId={customerId}
        onBack={() => (onBack ? onBack() : router.push("/dashboard/customers"))}
        backLabel="Back to Customers"
        homeHref="/dashboard/customers"
        homeLabel="Go to Customers"
        onRetry={fetchCustomer}
      />
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
            onClick={() =>
              onBack ? onBack() : router.push("/dashboard/customers")
            }
          >
            ← Back to Customers
          </Button>
        </div>
        <div className="flex items-center justify-between">
          <div>
            <PageTitle className="text-2xl font-bold">
              {customer.display_name}
            </PageTitle>
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

      {hasAnyRole(roles, ["admin"]) && (
        <PortalAccessPanel customerId={customer.customer_id} />
      )}

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
