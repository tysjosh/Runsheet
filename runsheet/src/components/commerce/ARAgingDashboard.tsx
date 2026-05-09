"use client";

import React, { useCallback, useEffect, useState } from "react";
import type { TenantAgingResponse, AgingSnapshot } from "../../services/commerceApi";
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
        <div className="animate-spin h-8 w-8 border-4 border-blue-600 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div role="alert" className="p-6">
        <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded">
          {error}
        </div>
      </div>
    );
  }

  if (!aging) return null;

  // Calculate bucket percentages for the chart
  const totalCents = aging.total_open_cents || 1;
  const buckets = [
    { label: "0–30 Days", cents: aging.bucket_0_30_cents, color: "bg-green-500" },
    { label: "31–60 Days", cents: aging.bucket_31_60_cents, color: "bg-yellow-500" },
    { label: "61–90 Days", cents: aging.bucket_61_90_cents, color: "bg-orange-500" },
    { label: "90+ Days", cents: aging.bucket_90_plus_cents, color: "bg-red-500" },
  ];

  return (
    <div className="p-6">
      <header className="mb-6">
        <h1 className="text-2xl font-bold">AR Aging Dashboard</h1>
        <p className="text-gray-600 mt-1">
          Accounts receivable aging overview with top outstanding accounts.
        </p>
      </header>

      {/* Summary stats */}
      <section aria-labelledby="summary-heading" className="mb-8">
        <h2 id="summary-heading" className="text-lg font-semibold mb-3">
          Summary
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Total Outstanding</p>
            <p className="text-2xl font-bold">{formatCents(aging.total_open_cents)}</p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Accounts with Balance</p>
            <p className="text-2xl font-bold">{aging.by_account.length}</p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">90+ Days Outstanding</p>
            <p className="text-2xl font-bold text-red-700">{formatCents(aging.bucket_90_plus_cents)}</p>
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
                  <p className="text-sm font-medium">{formatCents(bucket.cents)}</p>
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
          <table className="w-full border-collapse" role="table">
            <thead>
              <tr className="border-b bg-gray-50">
                <th className="text-left p-3 font-medium">Date</th>
                <th className="text-left p-3 font-medium">0–30</th>
                <th className="text-left p-3 font-medium">31–60</th>
                <th className="text-left p-3 font-medium">61–90</th>
                <th className="text-left p-3 font-medium">90+</th>
                <th className="text-left p-3 font-medium">Total</th>
              </tr>
            </thead>
            <tbody>
              {history.slice(0, 14).map((snap) => (
                <tr key={snap.snapshot_id} className="border-b">
                  <td className="p-3">{snap.snapshot_date}</td>
                  <td className="p-3">{formatCents(snap.bucket_0_30_cents)}</td>
                  <td className="p-3">{formatCents(snap.bucket_31_60_cents)}</td>
                  <td className="p-3">{formatCents(snap.bucket_61_90_cents)}</td>
                  <td className="p-3">{formatCents(snap.bucket_90_plus_cents)}</td>
                  <td className="p-3 font-medium">{formatCents(snap.total_open_cents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* Top 50 accounts table */}
      <section aria-labelledby="top-accounts-heading">
        <h2 id="top-accounts-heading" className="text-lg font-semibold mb-3">
          Top Accounts by Outstanding Balance
        </h2>
        {aging.by_account.length === 0 ? (
          <p className="text-gray-500">No accounts with outstanding balances.</p>
        ) : (
          <table className="w-full border-collapse" role="table">
            <thead>
              <tr className="border-b bg-gray-50">
                <th className="text-left p-3 font-medium">Account</th>
                <th className="text-left p-3 font-medium">0–30</th>
                <th className="text-left p-3 font-medium">31–60</th>
                <th className="text-left p-3 font-medium">61–90</th>
                <th className="text-left p-3 font-medium">90+</th>
                <th className="text-left p-3 font-medium">Total</th>
                <th className="text-left p-3 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {aging.by_account.slice(0, 50).map((acct) => (
                <tr key={acct.account_id} className="border-b hover:bg-gray-50">
                  <td className="p-3">{acct.display_name}</td>
                  <td className="p-3">{formatCents(acct.bucket_0_30_cents)}</td>
                  <td className="p-3">{formatCents(acct.bucket_31_60_cents)}</td>
                  <td className="p-3">{formatCents(acct.bucket_61_90_cents)}</td>
                  <td className="p-3 text-red-700 font-medium">
                    {formatCents(acct.bucket_90_plus_cents)}
                  </td>
                  <td className="p-3 font-bold">
                    {formatCents(acct.total_open_cents)}
                  </td>
                  <td className="p-3">
                    <button
                      type="button"
                      onClick={() => onViewAccount?.(acct.account_id)}
                      className="text-blue-600 hover:underline"
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
