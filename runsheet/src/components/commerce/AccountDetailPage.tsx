"use client";

import React, { useCallback, useEffect, useState } from "react";
import type {
  Account,
  AgingBuckets,
  CreditOverridePayload,
} from "../../services/commerceApi";
import {
  getAccount,
  getAccountAging,
  applyCreditOverride,
  deleteCreditOverride,
} from "../../services/commerceApi";

interface AccountDetailPageProps {
  accountId: string;
  onBack?: () => void;
  onViewCustomer?: (customerId: string) => void;
}

export default function AccountDetailPage({
  accountId,
  onBack,
  onViewCustomer,
}: AccountDetailPageProps) {
  const [account, setAccount] = useState<Account | null>(null);
  const [aging, setAging] = useState<AgingBuckets | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [overrideReason, setOverrideReason] = useState("");
  const [overrideExpiry, setOverrideExpiry] = useState("");
  const [overrideSubmitting, setOverrideSubmitting] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [accountRes, agingRes] = await Promise.all([
        getAccount(accountId),
        getAccountAging(accountId),
      ]);
      setAccount(accountRes.data);
      setAging(agingRes.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load account details",
      );
    } finally {
      setLoading(false);
    }
  }, [accountId]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleApplyOverride = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!overrideReason.trim()) return;
    setOverrideSubmitting(true);
    try {
      const payload: CreditOverridePayload = {
        reason: overrideReason,
        authorized_by: "current_user",
        expires_at: overrideExpiry || new Date(Date.now() + 7 * 86400000).toISOString(),
      };
      const res = await applyCreditOverride(accountId, payload);
      setAccount(res.data);
      setDrawerOpen(false);
      setOverrideReason("");
      setOverrideExpiry("");
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to apply credit override",
      );
    } finally {
      setOverrideSubmitting(false);
    }
  };

  const handleExpireOverride = async () => {
    try {
      const res = await deleteCreditOverride(accountId);
      setAccount(res.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to expire credit override",
      );
    }
  };

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12">
        <span className="sr-only">Loading account details...</span>
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

  if (!account) return null;

  const formatCents = (cents: number) =>
    `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;

  return (
    <div className="p-6">
      {/* Header */}
      <header className="mb-6">
        <div className="flex items-center gap-4 mb-2">
          {onBack && (
            <button
              type="button"
              onClick={onBack}
              className="text-blue-600 hover:underline"
            >
              ← Back to Accounts
            </button>
          )}
        </div>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">{account.display_name}</h1>
            <p className="text-gray-600">
              Tier: {account.tier} · Net Terms: {account.net_terms_days} days
            </p>
          </div>
          <div className="flex items-center gap-3">
            <span
              className={`px-3 py-1 rounded text-sm font-medium ${
                account.credit_state === "ok"
                  ? "bg-green-100 text-green-800"
                  : account.credit_state === "hold"
                    ? "bg-red-100 text-red-800"
                    : "bg-blue-100 text-blue-800"
              }`}
            >
              {account.credit_state}
            </span>
            <button
              type="button"
              onClick={() => setDrawerOpen(true)}
              className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700"
            >
              Credit Override
            </button>
          </div>
        </div>
      </header>

      {/* Credit summary cards */}
      <section aria-labelledby="credit-heading" className="mb-8">
        <h2 id="credit-heading" className="text-lg font-semibold mb-3">
          Credit Summary
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Credit Limit</p>
            <p className="text-2xl font-bold">
              {formatCents(account.credit_limit_cents)}
            </p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Open Balance</p>
            <p className="text-2xl font-bold">
              {formatCents(account.open_balance_cents)}
            </p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Credit Balance</p>
            <p className="text-2xl font-bold">
              {formatCents(account.credit_balance_cents)}
            </p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Net Terms</p>
            <p className="text-2xl font-bold">{account.net_terms_days} days</p>
          </div>
        </div>
        {account.credit_override_expires_at && (
          <div className="mt-3 p-3 bg-blue-50 border border-blue-200 rounded flex items-center justify-between">
            <span className="text-sm text-blue-800">
              Credit override active until{" "}
              {new Date(account.credit_override_expires_at).toLocaleDateString()}
            </span>
            <button
              type="button"
              onClick={handleExpireOverride}
              className="text-sm text-red-600 hover:underline"
            >
              Expire Now
            </button>
          </div>
        )}
      </section>

      {/* Aging bucket cards */}
      {aging && (
        <section aria-labelledby="aging-heading" className="mb-8">
          <h2 id="aging-heading" className="text-lg font-semibold mb-3">
            AR Aging Buckets
          </h2>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            <div className="border rounded p-4">
              <p className="text-sm text-gray-600">0–30 Days</p>
              <p className="text-xl font-bold">
                {formatCents(aging.bucket_0_30_cents)}
              </p>
            </div>
            <div className="border rounded p-4">
              <p className="text-sm text-gray-600">31–60 Days</p>
              <p className="text-xl font-bold">
                {formatCents(aging.bucket_31_60_cents)}
              </p>
            </div>
            <div className="border rounded p-4">
              <p className="text-sm text-gray-600">61–90 Days</p>
              <p className="text-xl font-bold">
                {formatCents(aging.bucket_61_90_cents)}
              </p>
            </div>
            <div className="border rounded p-4 bg-red-50">
              <p className="text-sm text-gray-600">90+ Days</p>
              <p className="text-xl font-bold text-red-700">
                {formatCents(aging.bucket_90_plus_cents)}
              </p>
            </div>
            <div className="border rounded p-4">
              <p className="text-sm text-gray-600">Total Open</p>
              <p className="text-xl font-bold">
                {formatCents(aging.total_open_cents)}
              </p>
            </div>
          </div>
        </section>
      )}

      {/* Customer link */}
      <section className="mb-8">
        <button
          type="button"
          onClick={() => onViewCustomer?.(account.customer_id)}
          className="text-blue-600 hover:underline"
        >
          View Parent Customer →
        </button>
      </section>

      {/* Credit Override Drawer */}
      {drawerOpen && (
        <div
          role="dialog"
          aria-labelledby="override-drawer-title"
          className="fixed inset-0 z-50 flex justify-end"
        >
          <div
            className="absolute inset-0 bg-black/30"
            onClick={() => setDrawerOpen(false)}
            onKeyDown={(e) => e.key === "Escape" && setDrawerOpen(false)}
            role="presentation"
          />
          <div className="relative bg-white w-full max-w-md h-full shadow-xl p-6 overflow-y-auto">
            <h2 id="override-drawer-title" className="text-xl font-bold mb-4">
              Apply Credit Override
            </h2>
            <form onSubmit={handleApplyOverride}>
              <div className="mb-4">
                <label
                  htmlFor="override-reason"
                  className="block text-sm font-medium mb-1"
                >
                  Reason
                </label>
                <textarea
                  id="override-reason"
                  value={overrideReason}
                  onChange={(e) => setOverrideReason(e.target.value)}
                  required
                  rows={3}
                  className="w-full border rounded px-3 py-2"
                  placeholder="Reason for credit override..."
                />
              </div>
              <div className="mb-6">
                <label
                  htmlFor="override-expiry"
                  className="block text-sm font-medium mb-1"
                >
                  Expires At
                </label>
                <input
                  id="override-expiry"
                  type="datetime-local"
                  value={overrideExpiry}
                  onChange={(e) => setOverrideExpiry(e.target.value)}
                  className="w-full border rounded px-3 py-2"
                />
              </div>
              <div className="flex gap-3">
                <button
                  type="submit"
                  disabled={overrideSubmitting || !overrideReason.trim()}
                  className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700 disabled:opacity-50"
                >
                  {overrideSubmitting ? "Applying..." : "Apply Override"}
                </button>
                <button
                  type="button"
                  onClick={() => setDrawerOpen(false)}
                  className="border px-4 py-2 rounded hover:bg-gray-50"
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
