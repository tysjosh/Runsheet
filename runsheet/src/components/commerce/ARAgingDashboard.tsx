"use client";

/**
 * Billing → AR aging (UI revamp task 3.4): no nested header. Totals go to the
 * hub's title row; the toolbar carries the bucket split (bar + labelled
 * amounts, so colour is never the only signal) and the History drawer; the
 * top-accounts DataTable is the page's list.
 */
import { Eye, History } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  Drawer,
  LoadErrorState,
  Skeleton,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { calendarDate, money, number, pct } from "../../lib/format";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import type {
  AgingSnapshot,
  TenantAgingResponse,
} from "../../services/commerceApi";
import { STATUS } from "../../styles/tokens";
import { PageTitle } from "../ui/PageHeader";

type AccountAging = TenantAgingResponse["by_account"][number];

import { getArAging, getArAgingHistory } from "../../services/commerceApi";

interface ARAgingDashboardProps {
  onViewAccount?: (accountId: string) => void;
}

export default function ARAgingDashboard({
  onViewAccount,
}: ARAgingDashboardProps) {
  const router = useRouter();
  const [aging, setAging] = useState<TenantAgingResponse | null>(null);
  const [history, setHistory] = useState<AgingSnapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Set only when the API refuses access (403): this dashboard is
  // platform_admin-only by design, so it gets a staff-access state rather
  // than an error banner.
  const [forbidden, setForbidden] = useState<LoadFailure | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    setForbidden(null);
    try {
      const [agingRes, historyRes] = await Promise.all([
        getArAging(),
        getArAgingHistory(),
      ]);
      // A payload without the account list (older API, empty tenant) must
      // not crash the tab.
      setAging({
        ...agingRes.data,
        by_account: agingRes.data?.by_account ?? [],
      });
      setHistory(historyRes.data);
    } catch (err) {
      const failure = classifyLoadError(err, "Failed to load AR aging data");
      if (failure.kind === "forbidden") {
        setForbidden(failure);
      } else {
        setError(failure.message);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const cents = (c: number) => money(c / 100);
  const counts = useMemo(
    () =>
      aging ? (
        <span className="whitespace-nowrap">
          {cents(aging.total_open_cents)} outstanding ·{" "}
          {number(aging.by_account.length)} accounts · 90+ days{" "}
          {cents(aging.bucket_90_plus_cents)}
        </span>
      ) : null,
    [aging],
  );
  const embedded = usePageChrome({ counts });
  const viewAccount = (accountId: string) =>
    onViewAccount
      ? onViewAccount(accountId)
      : router.push(
          `/dashboard/billing/accounts/${encodeURIComponent(accountId)}`,
        );

  const title = (
    <PageTitle className="text-base font-semibold text-text">
      AR Aging Dashboard
    </PageTitle>
  );
  const titleRow = embedded ? (
    <h2 className="sr-only">AR Aging Dashboard</h2>
  ) : (
    <div className="flex h-11 items-center gap-3 border-b border-slate-200 px-4">
      {title}
      <span className="text-xs text-text-muted">{counts}</span>
    </div>
  );

  if (loading) {
    return (
      <div className="p-4">
        <Skeleton rows={6} label="Loading AR aging data" />
      </div>
    );
  }

  if (forbidden) {
    return (
      <div>
        {embedded ? (
          <h2 className="px-4 pt-4 text-base font-semibold text-text">
            AR Aging Dashboard
          </h2>
        ) : (
          titleRow
        )}
        <div className="p-4">
          <LoadErrorState
            failure={forbidden}
            entityLabel="AR aging"
            onBack={() => router.push("/dashboard")}
            backLabel="Back to Today"
            homeHref="/dashboard/billing"
            homeLabel="Go to Billing"
            staffOnly
            embedded
          />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4">
        <LoadErrorState
          failure={{ kind: "error", message: error }}
          entityLabel="AR aging"
          onRetry={fetchData}
          embedded
        />
      </div>
    );
  }

  if (!aging) return null;

  const totalCents = aging.total_open_cents || 1;
  // Bucket hues follow the status scale (OK → Warning → Overdue → Critical).
  const buckets = [
    {
      label: "0–30 days",
      cents: aging.bucket_0_30_cents,
      color: STATUS.ok.dot,
    },
    {
      label: "31–60 days",
      cents: aging.bucket_31_60_cents,
      color: STATUS.warning.dot,
    },
    {
      label: "61–90 days",
      cents: aging.bucket_61_90_cents,
      color: STATUS.overdue.dot,
    },
    {
      label: "90+ days",
      cents: aging.bucket_90_plus_cents,
      color: STATUS.critical.dot,
    },
  ];

  const accountColumns: Column<AccountAging>[] = [
    {
      key: "display_name",
      header: "Account",
      truncate: true,
      title: (a) => a.display_name,
      className: "font-medium text-text",
      cell: (a) => a.display_name,
    },
    ...(
      [
        ["bucket_0_30_cents", "0–30"],
        ["bucket_31_60_cents", "31–60"],
        ["bucket_61_90_cents", "61–90"],
      ] as const
    ).map(([key, header]) => ({
      key,
      header,
      align: "right" as const,
      width: 130,
      className: "tabular-nums text-slate-700",
      cell: (a: AccountAging) => cents(a[key]),
    })),
    {
      key: "bucket_90_plus_cents",
      header: "90+",
      align: "right",
      width: 130,
      className: "tabular-nums font-medium text-red-800",
      cell: (a) => cents(a.bucket_90_plus_cents),
    },
    {
      key: "total_open_cents",
      header: "Total",
      align: "right",
      width: 140,
      className: "tabular-nums font-semibold text-text",
      cell: (a) => cents(a.total_open_cents),
    },
  ];

  const historyColumns: Column<AgingSnapshot>[] = [
    {
      key: "snapshot_date",
      header: "Date",
      cell: (h) => calendarDate(h.snapshot_date),
    },
    ...(
      [
        ["bucket_0_30_cents", "0–30"],
        ["bucket_31_60_cents", "31–60"],
        ["bucket_61_90_cents", "61–90"],
        ["bucket_90_plus_cents", "90+"],
        ["total_open_cents", "Total"],
      ] as const
    ).map(([key, header]) => ({
      key,
      header,
      align: "right" as const,
      className: "tabular-nums",
      cell: (h: AgingSnapshot) => cents(h[key]),
    })),
  ];

  return (
    <div className="flex h-full flex-col bg-surface">
      {titleRow}
      <Toolbar
        label="AR aging"
        filters={
          <div className="flex min-w-0 items-center gap-3">
            <div
              className="flex h-2.5 w-32 shrink-0 overflow-hidden rounded-full bg-slate-200"
              role="img"
              aria-label={`Aging bucket distribution chart: ${buckets
                .map(
                  (b) =>
                    `${b.label} ${pct(b.cents / totalCents, { fraction: true })}`,
                )
                .join(", ")}`}
            >
              {buckets.map((b) => {
                const share = (b.cents / totalCents) * 100;
                if (share < 0.5) return null;
                return (
                  <span
                    key={b.label}
                    style={{ width: `${share}%`, backgroundColor: b.color }}
                  />
                );
              })}
            </div>
            <ul className="flex min-w-0 items-center gap-3 overflow-hidden text-xs">
              {buckets.map((b) => (
                <li
                  key={b.label}
                  className="flex shrink-0 items-center gap-1.5 whitespace-nowrap"
                >
                  <span
                    aria-hidden="true"
                    className="h-2.5 w-2.5 rounded-sm"
                    style={{ backgroundColor: b.color }}
                  />
                  <span className="text-text-muted">{b.label}</span>
                  <span className="font-semibold tabular-nums text-text">
                    {cents(b.cents)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        }
        end={
          history.length > 0 ? (
            <Button
              size="sm"
              variant="ghost"
              icon={<History className="h-3.5 w-3.5" />}
              onClick={() => setHistoryOpen(true)}
            >
              History
            </Button>
          ) : null
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<AccountAging>
          ariaLabel="Top accounts by outstanding balance"
          columns={accountColumns}
          data={aging.by_account.slice(0, 50)}
          getRowId={(a) => a.account_id}
          rowLabel={(a) => a.display_name}
          onRowClick={(a) => viewAccount(a.account_id)}
          rowMenu={(a) => [
            {
              id: "view",
              label: "View account",
              icon: <Eye className="h-3.5 w-3.5" />,
              onSelect: () => viewAccount(a.account_id),
            },
          ]}
          emptyState={
            <p className="text-sm text-text-muted">
              No accounts with outstanding balances.
            </p>
          }
        />
      </div>
      <Drawer
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        title="Aging History"
        width={720}
      >
        <DataTable<AgingSnapshot>
          ariaLabel="Aging history"
          columns={historyColumns}
          data={history.slice(0, 14)}
          getRowId={(h) => h.snapshot_id}
        />
      </Drawer>
    </div>
  );
}
