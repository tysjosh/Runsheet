"use client";

/**
 * Account detail (UI revamp task 3.4): the detail template (title row with
 * back, credit badge and the Credit override action), facts through
 * `lib/format`, and the credit override as an sm FormDialog (design.md §5).
 */
import { useRouter } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import {
  Button,
  EntityLink,
  Field,
  FormDialog,
  INPUT_CLASS,
  InlineBanner,
  LoadErrorState,
  PageHeader,
  Skeleton,
} from "@/components/ui";
import { dateTime, humanize, money, number } from "../../lib/format";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
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
import { PageTitle } from "../ui/PageHeader";
import { AGING_LABELS } from "./agingLabels";
import { AccountStatusBadge, CreditStateBadge } from "./billingStatus";

interface AccountDetailPageProps {
  accountId: string;
  onBack?: () => void;
  onViewCustomer?: (customerId: string) => void;
}

type OverrideValues = { reason: string; expires: string };

/** `datetime-local` value for a Date in the browser's zone. */
function localInputValue(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

export function validateOverride(v: OverrideValues) {
  const errors: Record<string, string | undefined> = {};
  if (!v.reason.trim()) errors.reason = "Enter a reason.";
  if (v.expires) {
    const t = new Date(v.expires).getTime();
    if (Number.isNaN(t)) errors.expires = "Enter a valid date and time.";
    else if (t <= Date.now()) errors.expires = "Pick a time in the future.";
  }
  return errors;
}

export default function AccountDetailPage({
  accountId,
  onBack,
  onViewCustomer,
}: AccountDetailPageProps) {
  const router = useRouter();
  const [account, setAccount] = useState<Account | null>(null);
  const [aging, setAging] = useState<AgingBuckets | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Initial-fetch failures that get a dedicated state: 403 (accounts are
  // platform_admin-only by design) and 404. Action errors keep using `error`.
  const [loadFailure, setLoadFailure] = useState<LoadFailure | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [overrideOpen, setOverrideOpen] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    setLoadFailure(null);
    try {
      const [accountRes, agingRes] = await Promise.all([
        getAccount(accountId),
        getAccountAging(accountId),
      ]);
      setAccount(accountRes.data);
      setAging(agingRes.data);
    } catch (err) {
      const failure = classifyLoadError(err, "Failed to load account details");
      if (failure.kind === "forbidden" || failure.kind === "not_found") {
        setLoadFailure(failure);
      } else {
        setError(failure.message);
      }
    } finally {
      setLoading(false);
    }
  }, [accountId]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const submitOverride = async (v: OverrideValues) => {
    const payload: CreditOverridePayload = {
      reason: v.reason.trim(),
      authorized_by: "current_user",
      expires_at: v.expires
        ? new Date(v.expires).toISOString()
        : new Date(Date.now() + 7 * 86400000).toISOString(),
    };
    const res = await applyCreditOverride(accountId, payload);
    return res.data;
  };

  const handleExpireOverride = async () => {
    setActionError(null);
    try {
      const res = await deleteCreditOverride(accountId);
      setAccount(res.data);
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to expire credit override",
      );
    }
  };

  const back = () => (onBack ? onBack() : router.back());

  if (loading) {
    return (
      <div className="p-4">
        <Skeleton rows={6} label="Loading account details" />
      </div>
    );
  }

  if (loadFailure) {
    const state = (
      <LoadErrorState
        failure={loadFailure}
        entityLabel="Account"
        entityId={accountId}
        onBack={back}
        homeHref="/dashboard/billing"
        homeLabel="Go to Billing"
        staffOnly
      />
    );
    if (loadFailure.kind !== "forbidden") return state;
    return (
      <div>
        <header className="flex h-11 items-center gap-2 border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Account
          </PageTitle>
          <p className="font-mono text-xs text-text-muted">{accountId}</p>
        </header>
        {state}
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4">
        <LoadErrorState
          failure={{ kind: "error", message: error }}
          entityLabel="Account"
          entityId={accountId}
          onBack={back}
          onRetry={fetchData}
          embedded
        />
      </div>
    );
  }

  if (!account) return null;

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        host
        title={account.display_name}
        back={{ label: "Back", onClick: back }}
        badge={<CreditStateBadge state={account.credit_state} />}
        counts={
          <span className="whitespace-nowrap">
            {humanize(account.tier)} tier · Net {number(account.net_terms_days)}
          </span>
        }
        actions={
          <Button size="sm" onClick={() => setOverrideOpen(true)}>
            Credit override
          </Button>
        }
      />
      <div className="flex-1 overflow-auto p-4">
        {actionError && (
          <div role="alert" className="mb-3">
            <InlineBanner tone="critical">{actionError}</InlineBanner>
          </div>
        )}
        {account.credit_override_expires_at && (
          <div className="mb-4">
            <InlineBanner
              tone="warning"
              action={
                <Button
                  variant="danger"
                  size="sm"
                  onClick={handleExpireOverride}
                >
                  Expire now
                </Button>
              }
            >
              Credit override active until{" "}
              {dateTime(account.credit_override_expires_at)}
            </InlineBanner>
          </div>
        )}

        <section aria-labelledby="credit-heading" className="mb-6">
          <h2
            id="credit-heading"
            className="mb-2 text-sm font-semibold text-text"
          >
            Credit summary
          </h2>
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <Fact
              label="Credit limit"
              value={money(account.credit_limit_cents / 100)}
            />
            <Fact
              label="Open balance"
              value={money(account.open_balance_cents / 100)}
            />
            <Fact
              label="Available credit"
              value={money(account.available_credit_cents / 100)}
            />
            <Fact
              label="Credit balance"
              value={money(account.credit_balance_cents / 100)}
            />
            <Fact
              label="Net terms"
              value={`${number(account.net_terms_days)} days`}
            />
          </dl>
        </section>

        {aging && (
          <section aria-labelledby="aging-heading" className="mb-6">
            <h2
              id="aging-heading"
              className="mb-2 text-sm font-semibold text-text"
            >
              AR aging
            </h2>
            {/* Aged by days past due_date (F12); Current is not yet due. */}
            <dl className="grid grid-cols-2 gap-3 md:grid-cols-6">
              <Fact
                label={AGING_LABELS.current}
                value={money((aging.bucket_current_cents ?? 0) / 100)}
              />
              <Fact
                label={AGING_LABELS.d1_30}
                value={money(aging.bucket_0_30_cents / 100)}
              />
              <Fact
                label={AGING_LABELS.d31_60}
                value={money(aging.bucket_31_60_cents / 100)}
              />
              <Fact
                label={AGING_LABELS.d61_90}
                value={money(aging.bucket_61_90_cents / 100)}
              />
              <Fact
                label={AGING_LABELS.d90_plus}
                value={money(aging.bucket_90_plus_cents / 100)}
                tone="critical"
              />
              <Fact
                label="Total open"
                value={money(aging.total_open_cents / 100)}
              />
            </dl>
          </section>
        )}

        <section aria-labelledby="account-info-heading" className="mb-6">
          <h2
            id="account-info-heading"
            className="mb-2 text-sm font-semibold text-text"
          >
            Account information
          </h2>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-3 rounded-lg border border-slate-200 p-4 text-sm md:grid-cols-2">
            <Row label="Status">
              <AccountStatusBadge status={account.status} />
            </Row>
            <Row label="Parent customer">
              {onViewCustomer ? (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onViewCustomer(account.customer_id)}
                >
                  View parent customer →
                </Button>
              ) : (
                <EntityLink type="customer" id={account.customer_id} />
              )}
            </Row>
            <Row label="Account ID">
              <span className="font-mono text-xs">{account.account_id}</span>
            </Row>
            <Row label="Payment preference">
              {humanize(account.payment_method_preference)}
            </Row>
          </dl>
        </section>
      </div>

      {overrideOpen && (
        <FormDialog<OverrideValues, Account>
          open
          size="sm"
          title="Apply credit override"
          help="Lets this account order over its credit limit until the override expires."
          submitLabel="Apply override"
          successMessage="Credit override applied"
          initialValues={{
            reason: "",
            expires: localInputValue(new Date(Date.now() + 7 * 86400000)),
          }}
          validate={validateOverride}
          onSubmit={submitOverride}
          onSaved={(a) => setAccount(a)}
          onClose={() => setOverrideOpen(false)}
        >
          {({ values, set, errors }) => (
            <>
              <Field label="Reason" required error={errors.reason}>
                <textarea
                  id="override-reason"
                  value={values.reason}
                  onChange={(e) => set("reason", e.target.value)}
                  rows={3}
                  placeholder="Why this account may exceed its limit"
                  className={`${INPUT_CLASS} h-auto py-1.5`}
                />
              </Field>
              <Field
                label="Expires"
                help="Defaults to 7 days from now."
                error={errors.expires}
              >
                <input
                  id="override-expiry"
                  type="datetime-local"
                  value={values.expires}
                  onChange={(e) => set("expires", e.target.value)}
                  className={INPUT_CLASS}
                />
              </Field>
            </>
          )}
        </FormDialog>
      )}
    </div>
  );
}

function Fact({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "critical";
}) {
  return (
    <div
      className={`rounded-lg border px-3 py-2 ${
        tone === "critical"
          ? "border-red-300 bg-red-50"
          : "border-slate-200 bg-surface"
      }`}
    >
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd
        className={`text-lg font-semibold tabular-nums ${
          tone === "critical" ? "text-red-800" : "text-text"
        }`}
      >
        {value}
      </dd>
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
