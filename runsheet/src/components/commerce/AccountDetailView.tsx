"use client";

import { ArrowLeft, Building2, CreditCard, DollarSign, Edit2, TrendingUp } from "lucide-react";
import { useEffect, useState } from "react";
import {
  type Account,
  type AgingBuckets,
  type Invoice,
  getAccount,
  getAccountAging,
  getInvoices,
} from "../../services/commerceApi";
import { Badge, Button } from "../ui";

interface AccountDetailViewProps {
  accountId: string;
  onBack: () => void;
}

export default function AccountDetailView({
  accountId,
  onBack,
}: AccountDetailViewProps) {
  const [account, setAccount] = useState<Account | null>(null);
  const [aging, setAging] = useState<AgingBuckets | null>(null);
  const [recentInvoices, setRecentInvoices] = useState<Invoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchAccountDetails() {
      setLoading(true);
      setError(null);
      try {
        const [accountRes, agingRes, invoicesRes] = await Promise.all([
          getAccount(accountId),
          getAccountAging(accountId),
          getInvoices({ account_id: accountId, size: 5 }),
        ]);

        setAccount(accountRes.data);
        setAging(agingRes.data);
        setRecentInvoices(invoicesRes.data || []);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to load account details",
        );
      } finally {
        setLoading(false);
      }
    }

    fetchAccountDetails();
  }, [accountId]);

  if (loading) {
    return (
      <div className="p-6">
        <div className="flex justify-center py-12">
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      </div>
    );
  }

  if (error || !account) {
    return (
      <div className="p-6">
        <Button variant="ghost" onClick={onBack} className="mb-4">
          <ArrowLeft className="w-4 h-4 mr-2" />
          Back to Accounts
        </Button>
        <div className="bg-error-light border border-error-light text-error-dark p-4 rounded">
          {error || "Account not found"}
        </div>
      </div>
    );
  }

  const getCreditStateBadge = (state: string) => {
    if (state === "ok") return <Badge variant="success">OK</Badge>;
    if (state === "hold") return <Badge variant="error">Hold</Badge>;
    if (state === "override") return <Badge variant="info">Override</Badge>;
    return <Badge variant="warning">{state}</Badge>;
  };

  const getStatusBadge = (status: string) => {
    if (status === "active") return <Badge variant="success">Active</Badge>;
    if (status === "suspended") return <Badge variant="warning">Suspended</Badge>;
    if (status === "closed") return <Badge variant="default">Closed</Badge>;
    return <Badge variant="default">{status}</Badge>;
  };

  const formatCurrency = (cents: number) => {
    return `$${(cents / 100).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  };

  return (
    <div className="p-6">
      {/* Header */}
      <div className="mb-6">
        <Button variant="ghost" onClick={onBack} className="mb-4">
          <ArrowLeft className="w-4 h-4 mr-2" />
          Back to Accounts
        </Button>

        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 mb-2">
              {account.display_name}
            </h1>
            <div className="flex items-center gap-3">
              {getStatusBadge(account.status)}
              {getCreditStateBadge(account.credit_state)}
              <span className="text-sm text-gray-500">
                Account ID: {account.account_id}
              </span>
            </div>
          </div>
          <Button variant="secondary" size="sm">
            <Edit2 className="w-4 h-4 mr-2" />
            Edit Account
          </Button>
        </div>
      </div>

      {/* Key Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
        <div className="bg-white rounded-xl border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-2">
            <DollarSign className="w-4 h-4 text-gray-400" />
            <span className="text-sm text-gray-600">Credit Limit</span>
          </div>
          <p className="text-2xl font-bold text-gray-900">
            {formatCurrency(account.credit_limit_cents)}
          </p>
        </div>

        <div className="bg-white rounded-xl border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-2">
            <TrendingUp className="w-4 h-4 text-gray-400" />
            <span className="text-sm text-gray-600">Open Balance</span>
          </div>
          <p className="text-2xl font-bold text-gray-900">
            {formatCurrency(account.open_balance_cents)}
          </p>
        </div>

        <div className="bg-white rounded-xl border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-2">
            <CreditCard className="w-4 h-4 text-gray-400" />
            <span className="text-sm text-gray-600">Available Credit</span>
          </div>
          <p className="text-2xl font-bold text-success">
            {formatCurrency(account.available_credit_cents)}
          </p>
        </div>

        <div className="bg-white rounded-xl border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-2">
            <Building2 className="w-4 h-4 text-gray-400" />
            <span className="text-sm text-gray-600">Credit Balance</span>
          </div>
          <p className="text-2xl font-bold text-info">
            {formatCurrency(account.credit_balance_cents)}
          </p>
        </div>
      </div>

      {/* Account Details & AR Aging */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        {/* Account Information */}
        <div className="bg-white rounded-xl border border-gray-200 p-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">
            Account Information
          </h2>
          <dl className="space-y-3">
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">Tier</dt>
              <dd className="text-sm font-medium text-gray-900 capitalize">
                {account.tier}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">Net Terms</dt>
              <dd className="text-sm font-medium text-gray-900">
                {account.net_terms_days} days
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">Payment Method</dt>
              <dd className="text-sm font-medium text-gray-900 capitalize">
                {account.payment_method_preference}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">Created</dt>
              <dd className="text-sm font-medium text-gray-900">
                {formatDate(account.created_at)}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">Last Updated</dt>
              <dd className="text-sm font-medium text-gray-900">
                {formatDate(account.updated_at)}
              </dd>
            </div>
            {account.credit_override_expires_at && (
              <div className="flex justify-between">
                <dt className="text-sm text-gray-600">Override Expires</dt>
                <dd className="text-sm font-medium text-warning-dark">
                  {formatDate(account.credit_override_expires_at)}
                </dd>
              </div>
            )}
          </dl>
        </div>

        {/* AR Aging */}
        {aging && (
          <div className="bg-white rounded-xl border border-gray-200 p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-4">
              AR Aging
            </h2>
            <dl className="space-y-3">
              <div className="flex justify-between">
                <dt className="text-sm text-gray-600">Current (0-30 days)</dt>
                <dd className="text-sm font-medium text-gray-900">
                  {formatCurrency(aging.bucket_0_30_cents)}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-sm text-gray-600">31-60 days</dt>
                <dd className="text-sm font-medium text-warning-dark">
                  {formatCurrency(aging.bucket_31_60_cents)}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-sm text-gray-600">61-90 days</dt>
                <dd className="text-sm font-medium text-warning-dark">
                  {formatCurrency(aging.bucket_61_90_cents)}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-sm text-gray-600">90+ days</dt>
                <dd className="text-sm font-medium text-error-dark">
                  {formatCurrency(aging.bucket_90_plus_cents)}
                </dd>
              </div>
              <div className="flex justify-between pt-3 border-t border-gray-200">
                <dt className="text-sm font-semibold text-gray-900">
                  Total Open
                </dt>
                <dd className="text-sm font-bold text-gray-900">
                  {formatCurrency(aging.total_open_cents)}
                </dd>
              </div>
            </dl>
          </div>
        )}
      </div>

      {/* Billing Address */}
      {account.billing_address && (
        <div className="bg-white rounded-xl border border-gray-200 p-6 mb-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">
            Billing Address
          </h2>
          <address className="not-italic text-sm text-gray-700">
            {account.billing_address.line1}
            <br />
            {account.billing_address.line2 && (
              <>
                {account.billing_address.line2}
                <br />
              </>
            )}
            {account.billing_address.city}, {account.billing_address.state}{" "}
            {account.billing_address.zip}
            <br />
            {account.billing_address.country}
          </address>
        </div>
      )}

      {/* Recent Invoices */}
      <div className="bg-white rounded-xl border border-gray-200 p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">
            Recent Invoices
          </h2>
          <Button variant="ghost" size="sm">
            View All
          </Button>
        </div>

        {recentInvoices.length === 0 ? (
          <p className="text-sm text-gray-500 py-4">No invoices found</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-200">
                  <th className="text-left text-xs font-medium text-gray-600 pb-3">
                    Invoice #
                  </th>
                  <th className="text-left text-xs font-medium text-gray-600 pb-3">
                    Status
                  </th>
                  <th className="text-left text-xs font-medium text-gray-600 pb-3">
                    Issued
                  </th>
                  <th className="text-left text-xs font-medium text-gray-600 pb-3">
                    Due Date
                  </th>
                  <th className="text-right text-xs font-medium text-gray-600 pb-3">
                    Total
                  </th>
                  <th className="text-right text-xs font-medium text-gray-600 pb-3">
                    Remaining
                  </th>
                </tr>
              </thead>
              <tbody>
                {recentInvoices.map((invoice) => (
                  <tr
                    key={invoice.invoice_id}
                    className="border-b border-gray-100 last:border-0"
                  >
                    <td className="py-3 text-sm font-medium text-gray-900">
                      {invoice.invoice_number}
                    </td>
                    <td className="py-3">
                      <Badge
                        variant={
                          invoice.status === "paid"
                            ? "success"
                            : invoice.status === "overdue"
                              ? "error"
                              : "default"
                        }
                      >
                        {invoice.status}
                      </Badge>
                    </td>
                    <td className="py-3 text-sm text-gray-700">
                      {formatDate(invoice.issued_at)}
                    </td>
                    <td className="py-3 text-sm text-gray-700">
                      {formatDate(invoice.due_date)}
                    </td>
                    <td className="py-3 text-sm text-gray-900 text-right">
                      {formatCurrency(invoice.total_cents)}
                    </td>
                    <td className="py-3 text-sm font-medium text-gray-900 text-right">
                      {formatCurrency(invoice.remaining_cents)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
