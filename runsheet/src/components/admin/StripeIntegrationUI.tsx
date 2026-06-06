"use client";

import {
  AlertCircle,
  Check,
  CreditCard,
  DollarSign,
  ExternalLink,
  RefreshCw,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  getStripePayments,
  getStripePublicConfig,
  type StripePaymentItem,
} from "../../services/adminApi";
import {
  Badge,
  Button,
  type Column,
  EntityLink,
  PageHeader,
  Table,
} from "../ui";

// ─── Helper Functions ────────────────────────────────────────────────────────

function formatAmount(
  amount: number | null | undefined,
  currency: string | null | undefined,
): string {
  if (amount === null || amount === undefined) return "—";
  const dollars = amount / 100;
  const currencySymbol =
    currency?.toUpperCase() === "USD" ? "$" : currency || "";
  return `${currencySymbol}${dollars.toFixed(2)}`;
}

function formatTimestamp(timestamp: number | null | undefined): string {
  if (!timestamp) return "—";
  return new Date(timestamp * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function getStatusBadge(status: string | null | undefined) {
  if (!status) return <Badge variant="default">Unknown</Badge>;

  switch (status.toLowerCase()) {
    case "succeeded":
      return <Badge variant="success">Succeeded</Badge>;
    case "processing":
      return <Badge variant="info">Processing</Badge>;
    case "requires_payment_method":
    case "requires_confirmation":
    case "requires_action":
      return <Badge variant="warning">Requires Action</Badge>;
    case "canceled":
      return <Badge variant="default">Canceled</Badge>;
    case "failed":
      return <Badge variant="error">Failed</Badge>;
    default:
      return <Badge variant="default">{status}</Badge>;
  }
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function StripeIntegrationUI() {
  const [publishableKey, setPublishableKey] = useState<string | null>(null);
  const [payments, setPayments] = useState<StripePaymentItem[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [isConfigured, setIsConfigured] = useState(false);

  const [limit, setLimit] = useState(20);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const fetchConfig = useCallback(async () => {
    try {
      const config = await getStripePublicConfig();
      setPublishableKey(config.publishable_key);
      setIsConfigured(true);
    } catch (err: any) {
      if (err?.status === 404 || err?.message?.includes("not_configured")) {
        setIsConfigured(false);
        setError("Stripe integration is not configured for this tenant");
      } else {
        setError(
          err instanceof Error
            ? err.message
            : "Failed to load Stripe configuration",
        );
      }
    }
  }, []);

  const fetchPayments = useCallback(
    async (cursor?: string) => {
      setLoading(true);
      setError(null);
      try {
        const params: any = { limit };
        if (cursor) params.starting_after = cursor;
        if (dateFrom) params.created_gte = new Date(dateFrom).toISOString();
        if (dateTo) params.created_lte = new Date(dateTo).toISOString();

        const response = await getStripePayments(params);
        setPayments(response.items);
        setHasMore(response.has_more);
        setNextCursor(response.next_starting_after || null);
      } catch (err: any) {
        if (err?.status === 404 || err?.message?.includes("not_configured")) {
          setIsConfigured(false);
          setError("Stripe integration is not configured for this tenant");
        } else {
          setError(
            err instanceof Error
              ? err.message
              : "Failed to load Stripe payments",
          );
        }
      } finally {
        setLoading(false);
      }
    },
    [limit, dateFrom, dateTo],
  );

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);

  useEffect(() => {
    if (isConfigured) {
      fetchPayments();
    }
  }, [isConfigured, fetchPayments]);

  const handleRefresh = () => {
    fetchPayments();
  };

  const handleLoadMore = () => {
    if (nextCursor) {
      fetchPayments(nextCursor);
    }
  };

  const paymentColumns: Column<StripePaymentItem>[] = [
    {
      key: "id",
      label: "Payment ID",
      render: (payment) => (
        <div className="flex items-center gap-2">
          <CreditCard className="w-4 h-4 text-primary" />
          <span className="font-mono text-sm">{payment.id}</span>
        </div>
      ),
    },
    {
      key: "created",
      label: "Created",
      render: (payment) => (
        <span className="text-sm text-gray-600">
          {formatTimestamp(payment.created)}
        </span>
      ),
    },
    {
      key: "amount",
      label: "Amount",
      render: (payment) => (
        <span className="text-sm font-medium text-gray-900">
          {formatAmount(payment.amount, payment.currency)}
        </span>
      ),
    },
    {
      key: "status",
      label: "Status",
      render: (payment) => getStatusBadge(payment.status),
    },
    {
      key: "customer",
      label: "Customer",
      render: (payment) => (
        <span className="text-sm text-gray-700">{payment.customer || "—"}</span>
      ),
    },
    {
      key: "description",
      label: "Description",
      render: (payment) => (
        <span className="text-sm text-gray-700">
          {payment.description || "—"}
        </span>
      ),
    },
    {
      key: "mapping",
      label: "Canonical Mapping",
      render: (payment) => {
        // An external Stripe charge maps to a canonical commerce payment, or
        // is explicitly flagged "Unmapped" rather than rendered as a dead id
        // (cross-module-entity-linkage Req 12.3).
        if (payment.mapping_status !== "mapped") {
          return (
            <Badge variant="warning" size="sm">
              Unmapped
            </Badge>
          );
        }
        return (
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <Badge variant="success" size="sm">
                Mapped
              </Badge>
              {payment.canonical_payment_id && (
                <span className="font-mono text-xs text-gray-500">
                  {payment.canonical_payment_id}
                </span>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              <EntityLink
                type="invoice"
                id={payment.invoice_id}
                label={payment.invoice_id ?? undefined}
              />
              <EntityLink
                type="account"
                id={payment.account_id}
                label={payment.account_id ?? undefined}
              />
            </div>
          </div>
        );
      },
    },
    {
      key: "actions",
      label: "Actions",
      render: (payment) => (
        <a
          href={`https://dashboard.stripe.com/payments/${payment.id}`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm text-blue-600 hover:text-blue-800 flex items-center gap-1"
        >
          View in Stripe
          <ExternalLink className="w-3 h-3" />
        </a>
      ),
    },
  ];

  return (
    <div className="p-6">
      <PageHeader
        title="Stripe Integration"
        subtitle="Monitor Stripe payments and configuration"
        icon={<DollarSign className="w-5 h-5" />}
      />

      {/* Error/Success Messages */}
      {error && (
        <div className="bg-error-light border border-error text-error-dark p-3 rounded-lg mb-4 flex items-center gap-2">
          <AlertCircle className="w-5 h-5 flex-shrink-0" />
          <span className="text-sm">{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            className="ml-auto"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {success && (
        <div className="bg-success-light border border-success text-success-dark p-3 rounded-lg mb-4 flex items-center gap-2">
          <Check className="w-5 h-5 flex-shrink-0" />
          <span className="text-sm">{success}</span>
          <button
            type="button"
            onClick={() => setSuccess(null)}
            className="ml-auto"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Configuration Status */}
      {!isConfigured ? (
        <div className="bg-white rounded-xl border border-gray-200 p-12 text-center mb-6">
          <AlertCircle className="w-12 h-12 text-gray-400 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-gray-900 mb-2">
            Stripe Not Configured
          </h3>
          <p className="text-gray-600 mb-6">
            Connect your Stripe account from the Integration Marketplace to
            start accepting payments.
          </p>
          <Button
            onClick={() => {
              window.location.href = "/dashboard";
            }}
          >
            Go to Integrations
          </Button>
        </div>
      ) : (
        <>
          {/* Configuration Info */}
          <div className="bg-white rounded-xl border border-gray-200 p-6 mb-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-4">
              Configuration
            </h3>
            <div className="space-y-3">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Publishable Key
                </label>
                <div className="font-mono text-sm bg-gray-50 p-3 rounded border border-gray-200">
                  {publishableKey || "Loading..."}
                </div>
              </div>
              <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 flex items-start gap-2">
                <AlertCircle className="w-4 h-4 text-blue-600 flex-shrink-0 mt-0.5" />
                <div className="text-sm text-blue-900">
                  <p className="font-medium mb-1">About Stripe Integration</p>
                  <p>
                    This integration allows you to accept payments through
                    Stripe. The secret key and webhook secret are securely
                    stored and never exposed through this UI.
                  </p>
                </div>
              </div>
            </div>
          </div>

          {/* Filters */}
          <div className="bg-white rounded-xl border border-gray-200 p-4 mb-6">
            <div className="flex flex-wrap gap-4 items-end">
              <div className="flex-1 min-w-[150px]">
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  From Date
                </label>
                <input
                  type="date"
                  value={dateFrom}
                  onChange={(e) => setDateFrom(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
                />
              </div>

              <div className="flex-1 min-w-[150px]">
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  To Date
                </label>
                <input
                  type="date"
                  value={dateTo}
                  onChange={(e) => setDateTo(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
                />
              </div>

              <div className="flex-1 min-w-[150px]">
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  Limit
                </label>
                <select
                  value={limit}
                  onChange={(e) => setLimit(Number(e.target.value))}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
                >
                  <option value={10}>10</option>
                  <option value={20}>20</option>
                  <option value={50}>50</option>
                  <option value={100}>100</option>
                </select>
              </div>

              <Button onClick={handleRefresh} disabled={loading}>
                <RefreshCw className="w-4 h-4 mr-2" />
                Refresh
              </Button>
            </div>
          </div>

          {/* Payments Table */}
          <div className="bg-white rounded-xl border border-gray-200">
            <div className="p-6 border-b border-gray-200">
              <h3 className="text-lg font-semibold text-gray-900">
                Recent Payments
              </h3>
              <p className="text-sm text-gray-600 mt-1">
                {payments.length} payment{payments.length !== 1 ? "s" : ""}{" "}
                loaded
              </p>
            </div>

            <div className="p-6">
              {loading && payments.length === 0 ? (
                <div className="flex justify-center py-12">
                  <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
                </div>
              ) : payments.length === 0 ? (
                <div className="text-center py-12 text-gray-500">
                  <CreditCard className="w-12 h-12 text-gray-400 mx-auto mb-4" />
                  <p>No payments found</p>
                </div>
              ) : (
                <>
                  <Table columns={paymentColumns} data={payments} />
                  {hasMore && (
                    <div className="mt-4 text-center">
                      <Button
                        variant="secondary"
                        onClick={handleLoadMore}
                        disabled={loading}
                      >
                        {loading ? "Loading..." : "Load More"}
                      </Button>
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
