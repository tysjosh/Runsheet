"use client";

import { useCallback, useEffect, useState } from "react";
import { Button, PageHeader, Table } from "@/components/ui";
import type {
  AgingSnapshot,
  TenantAgingResponse,
} from "../../services/commerceApi";
import { getArAging, getArAgingHistory } from "../../services/commerceApi";

interface ARAgingDashboardProps {
  onViewAccount?: (accountId: string) => void;
}

export default function ARAgingDashboard({
  onViewAccount,
}: ARAgingDashboardProps) {
  const [aging, setAging] = useState<TenantAgingResponse | null>(null);
  const [history, setHistory] = useState<AgingSnapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [agingRes, historyRes] = await Promise.all([
        getArAging(),
        getArAgingHistory(),
      ]);
      setAging(agingRes.data);
      setHistory(historyRes.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load AR aging data",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const formatCents = (cents: number) =>
    `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12">
        <span className="sr-only">Loading AR aging data...</span>
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

  if (!aging) return null;

  // Calculate bucket percentages for the chart
  const totalCents = aging.total_open_cents || 1;
  const buckets = [
    {
      label: "0–30 Days",
      cents: aging.bucket_0_30_cents,
      color: "bg-success-light0",
    },
    {
      label: "31–60 Days",
      cents: aging.bucket_31_60_cents,
      color: "bg-warning-light0",
    },
    {
      label: "61–90 Days",
      cents: aging.bucket_61_90_cents,
      color: "bg-warning-light0",
    },
    {
      label: "90+ Days",
      cents: aging.bucket_90_plus_cents,
      color: "bg-error-light0",
    },
  ];

  return (
    <div className="p-6">
      <PageHeader
        title="AR Aging Dashboard"
        subtitle="Accounts receivable aging overview with top outstanding accounts.
        "
      />

      {/* Summary stats */}
      <section aria-labelledby="summary-heading" className="mb-8">
        <h2 id="summary-heading" className="text-lg font-semibold mb-3">
          Summary
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Total Outstanding</p>
            <p className="text-2xl font-bold">
              {formatCents(aging.total_open_cents)}
            </p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Accounts with Balance</p>
            <p className="text-2xl font-bold">{aging.by_account.length}</p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">90+ Days Outstanding</p>
            <p className="text-2xl font-bold text-error-dark">
              {formatCents(aging.bucket_90_plus_cents)}
            </p>
          </div>
        </div>
      </section>

      {/* Bucket chart (horizontal bar) */}
      <section aria-labelledby="bucket-chart-heading" className="mb-8">
        <h2 id="bucket-chart-heading" className="text-lg font-semibold mb-3">
          Aging Buckets
        </h2>
        <div className="border rounded p-4">
          {/* Stacked bar */}
          <div
            className="flex h-10 rounded overflow-hidden mb-4"
            role="img"
            aria-label="Aging bucket distribution chart"
          >
            {buckets.map((bucket) => {
              const pct = (bucket.cents / totalCents) * 100;
              if (pct < 0.5) return null;
              return (
                <div
                  key={bucket.label}
                  className={`${bucket.color} flex items-center justify-center text-white text-xs font-medium`}
                  style={{ width: `${pct}%` }}
                  title={`${bucket.label}: ${formatCents(bucket.cents)} (${pct.toFixed(1)}%)`}
                >
                  {pct > 8 ? `${pct.toFixed(0)}%` : ""}
                </div>
              );
            })}
          </div>

          {/* Legend */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {buckets.map((bucket) => (
              <div key={bucket.label} className="flex items-center gap-2">
                <span className={`w-3 h-3 rounded ${bucket.color}`} />
                <div>
                  <p className="text-xs text-gray-600">{bucket.label}</p>
                  <p className="text-sm font-medium">
                    {formatCents(bucket.cents)}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* History trend */}
      {history.length > 0 && (
        <section aria-labelledby="history-heading" className="mb-8">
          <h2 id="history-heading" className="text-lg font-semibold mb-3">
            Aging History
          </h2>
          <Table
            columns={[
              { key: "snapshot_date", label: "Date" },
              {
                key: "bucket_0_30_cents",
                label: "0–30",
                render: (snap) => formatCents(snap.bucket_0_30_cents),
              },
              {
                key: "bucket_31_60_cents",
                label: "31–60",
                render: (snap) => formatCents(snap.bucket_31_60_cents),
              },
              {
                key: "bucket_61_90_cents",
                label: "61–90",
                render: (snap) => formatCents(snap.bucket_61_90_cents),
              },
              {
                key: "bucket_90_plus_cents",
                label: "90+",
                render: (snap) => formatCents(snap.bucket_90_plus_cents),
              },
              {
                key: "total_open_cents",
                label: "Total",
                render: (snap) => (
                  <span className="font-medium">
                    {formatCents(snap.total_open_cents)}
                  </span>
                ),
              },
            ]}
            data={history.slice(0, 14)}
            keyExtractor={(snap) => snap.snapshot_id}
          />
        </section>
      )}

      {/* Top 50 accounts table */}
      <section aria-labelledby="top-accounts-heading">
        <h2 id="top-accounts-heading" className="text-lg font-semibold mb-3">
          Top Accounts by Outstanding Balance
        </h2>
        {aging.by_account.length === 0 ? (
          <p className="text-gray-500">
            No accounts with outstanding balances.
          </p>
        ) : (
          <Table
            columns={[
              { key: "display_name", label: "Account" },
              {
                key: "bucket_0_30_cents",
                label: "0–30",
                render: (acct) => formatCents(acct.bucket_0_30_cents),
              },
              {
                key: "bucket_31_60_cents",
                label: "31–60",
                render: (acct) => formatCents(acct.bucket_31_60_cents),
              },
              {
                key: "bucket_61_90_cents",
                label: "61–90",
                render: (acct) => formatCents(acct.bucket_61_90_cents),
              },
              {
                key: "bucket_90_plus_cents",
                label: "90+",
                render: (acct) => (
                  <span className="text-error-dark font-medium">
                    {formatCents(acct.bucket_90_plus_cents)}
                  </span>
                ),
              },
              {
                key: "total_open_cents",
                label: "Total",
                render: (acct) => (
                  <span className="font-bold">
                    {formatCents(acct.total_open_cents)}
                  </span>
                ),
              },
              {
                key: "actions",
                label: "Actions",
                render: (acct) => (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onViewAccount?.(acct.account_id)}
                  >
                    View
                  </Button>
                ),
              },
            ]}
            data={aging.by_account.slice(0, 50)}
            keyExtractor={(acct) => acct.account_id}
          />
        )}
      </section>
    </div>
  );
}
