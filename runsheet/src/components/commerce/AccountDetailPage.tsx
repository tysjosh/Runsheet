"use client";

import type React from "react";
import { useCallback, useEffect, useState } from "react";
import { Badge, Button } from "@/components/ui";
import type {
  Account,
  AgingBuckets,
  CreditOverridePayload,
} from "../../services/commerceApi";
import {
  applyCreditOverride,
  deleteCreditOverride,
  getAccount,
  getAccountAging,
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
        expires_at:
          overrideExpiry || new Date(Date.now() + 7 * 86400000).toISOString(),
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

  const getCreditStateVariant = (
    state: string,
  ): "success" | "error" | "info" => {
    if (state === "ok") return "success";
    if (state === "hold") return "error";
    return "info";
  };

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12">
        <span className="sr-only">Loading account details...</span>
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

  if (!account) return null;

  const formatCents = (cents: number) =>
    `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;

  return (
    <div className="p-6">
      {/* Header */}
      <header className="mb-6">
        <div className="flex items-center gap-4 mb-2">
          {onBack && (
            <Button variant="ghost" onClick={onBack}>
              ← Back to Accounts
            </Button>
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
            <Badge variant={getCreditStateVariant(account.credit_state)}>
              {account.credit_state}
            </Badge>
            <Button onClick={() => setDrawerOpen(true)}>Credit Override</Button>
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
          <div className="mt-3 p-3 bg-info-light border border-info rounded flex items-center justify-between">
            <span className="text-sm text-info-dark">
              Credit override active until{" "}
              {new Date(
                account.credit_override_expires_at,
              ).toLocaleDateString()}
            </span>
            <Button variant="danger" size="sm" onClick={handleExpireOverride}>
              Expire Now
            </Button>
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
            <div className="border rounded p-4 bg-error-light">
              <p className="text-sm text-gray-600">90+ Days</p>
              <p className="text-xl font-bold text-error-dark">
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
        <Button
          variant="ghost"
          onClick={() => onViewCustomer?.(account.customer_id)}
        >
          View Parent Customer →
        </Button>
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
                <Button
                  type="submit"
                  disabled={overrideSubmitting || !overrideReason.trim()}
                  loading={overrideSubmitting}
                >
                  Apply Override
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => setDrawerOpen(false)}
                >
                  Cancel
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
