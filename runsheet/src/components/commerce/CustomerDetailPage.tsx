"use client";

import { useRouter } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import {
  LoadErrorState,
  PageHeader,
  Skeleton,
  StatusBadge,
} from "@/components/ui";
import { hasAnyRole } from "../../config/modules";
import { dateLong, money, number } from "../../lib/format";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import {
  type CustomerWithProjections,
  getCustomer,
} from "../../services/commerceApi";
import { getCurrentUserRoles } from "../../utils/auth";
import { CUSTOMER_STATUS } from "./customerStatus";
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

  if (loading) {
    return (
      <div className="p-4">
        <Skeleton rows={6} label="Loading customer details" />
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

  const back = () => (onBack ? onBack() : router.push("/dashboard/customers"));
  const cfg = CUSTOMER_STATUS[customer.status] ?? CUSTOMER_STATUS.active;

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        host
        title={customer.display_name}
        back={{ label: "Back to Customers", onClick: back }}
        badge={<StatusBadge status={cfg.status} label={cfg.label} />}
        counts={
          <span className="whitespace-nowrap text-xs text-text-muted">
            {number(customer.account_count)} accounts ·{" "}
            {number(customer.open_invoice_count)} open invoices
          </span>
        }
      />
      <div className="flex-1 overflow-auto p-4">
        <section aria-labelledby="summary-heading" className="mb-6">
          <h2 id="summary-heading" className="sr-only">
            Customer summary
          </h2>
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Fact label="Accounts" value={number(customer.account_count)} />
            <Fact
              label="Open invoices"
              value={number(customer.open_invoice_count)}
            />
            <Fact
              label="Open balance"
              value={money(customer.open_balance_cents / 100)}
            />
            <Fact
              label="Lifetime revenue"
              value={money(customer.lifetime_revenue_cents / 100)}
            />
          </dl>
        </section>

        <section aria-labelledby="info-heading" className="mb-6">
          <h2
            id="info-heading"
            className="mb-2 text-sm font-semibold text-text"
          >
            Customer information
          </h2>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-3 rounded-lg border border-slate-200 p-4 text-sm md:grid-cols-2">
            <Row label="Display name">{customer.display_name}</Row>
            {customer.legal_name && (
              <Row label="Legal name">{customer.legal_name}</Row>
            )}
            {customer.primary_email && (
              <Row label="Primary email">{customer.primary_email}</Row>
            )}
            {customer.tax_id && <Row label="Tax ID">{customer.tax_id}</Row>}
            <Row label="Customer ID">
              <span className="font-mono text-xs">{customer.customer_id}</span>
            </Row>
            <Row label="Created">{dateLong(customer.created_at)}</Row>
            <Row label="Last updated">{dateLong(customer.updated_at)}</Row>
          </dl>
        </section>

        {hasAnyRole(roles, ["admin"]) && (
          <PortalAccessPanel customerId={customer.customer_id} />
        )}
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2">
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums text-text">{value}</dd>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="font-medium text-text">{children}</dd>
    </div>
  );
}
