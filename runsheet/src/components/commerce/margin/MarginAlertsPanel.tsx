"use client";

/**
 * Margin alerts (RevenueGuard's admin channel), grouped by order. Alerts with
 * no order (recompute digests) are listed under "Other". Approving a leakage
 * proposal only records the decision; nothing changes prices (FR5.9).
 */
import { useCallback, useEffect, useState } from "react";
import {
  getMarginAlerts,
  type MarginAlert,
  resolveMarginAlert,
} from "../../../services/marginApi";
import { Button } from "../../ui";

export const ALERT_TYPE_LABELS: Record<MarginAlert["alert_type"], string> = {
  negative_margin: "Negative margin",
  missing_cost: "Missing cost",
  leakage_proposal: "Repeated sales below floor: review pricing",
  recompute_digest: "Recompute finished with flagged records",
};

const ACTIVE_STATUSES: MarginAlert["status"][] = [
  "open",
  "acknowledged",
  "pending_review",
];

type Action = "acknowledge" | "approve" | "dismiss";

export function groupAlertsByOrder(
  alerts: MarginAlert[],
): [string, MarginAlert[]][] {
  const groups = new Map<string, MarginAlert[]>();
  for (const alert of alerts) {
    const key = alert.order_id ?? "Other";
    groups.set(key, [...(groups.get(key) ?? []), alert]);
  }
  // "Other" last, orders in first-seen (newest) order.
  return [...groups.entries()].sort(
    ([a], [b]) => Number(a === "Other") - Number(b === "Other"),
  );
}

export interface MarginAlertsPanelProps {
  onChanged?: () => void;
}

export default function MarginAlertsPanel({
  onChanged,
}: MarginAlertsPanelProps) {
  const [alerts, setAlerts] = useState<MarginAlert[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const page = await getMarginAlerts({
        status: ACTIVE_STATUSES,
        limit: 200,
      });
      setAlerts(page.items);
    } catch {
      setError("Margin alerts could not be loaded. Try again.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (alert: MarginAlert, action: Action) => {
    setBusy(alert.alert_id);
    setError(null);
    setStatus("");
    try {
      await resolveMarginAlert(alert.alert_id, action);
      setStatus(
        `Alert ${action === "acknowledge" ? "acknowledged" : action === "approve" ? "approved" : "dismissed"}`,
      );
      await load();
      onChanged?.();
    } catch {
      setError(
        "The alert could not be updated. It may have changed; the list was refreshed.",
      );
      await load();
    } finally {
      setBusy(null);
    }
  };

  return (
    <section aria-labelledby="margin-alerts-heading" className="space-y-4">
      <h2 id="margin-alerts-heading" className="text-lg font-semibold">
        Margin alerts
      </h2>
      <p role="alert" className={error ? "text-sm text-error" : "sr-only"}>
        {error ?? ""}
      </p>
      <p role="status" className="sr-only">
        {status}
      </p>
      {alerts.length === 0 && !error && (
        <p className="text-sm text-gray-500">No open margin alerts.</p>
      )}
      {groupAlertsByOrder(alerts).map(([order, items]) => (
        <div key={order} className="rounded border border-gray-200">
          <h3 className="px-3 py-2 text-sm font-semibold bg-gray-50">
            {order === "Other" ? "Other" : `Order ${order}`}
          </h3>
          <ul className="divide-y">
            {items.map((alert) => (
              <li
                key={alert.alert_id}
                className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm"
              >
                <span>
                  <span className="font-medium">
                    {ALERT_TYPE_LABELS[alert.alert_type] ?? alert.alert_type}
                  </span>
                  <span className="ml-2 text-gray-600">
                    {alert.severity} · {alert.status.replace("_", " ")} ·{" "}
                    {alert.created_at.slice(0, 10)}
                    {alert.customer_id ? ` · ${alert.customer_id}` : ""}
                    {alert.product_code ? ` · ${alert.product_code}` : ""}
                  </span>
                </span>
                <span className="flex gap-2">
                  {alert.status === "open" && (
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      loading={busy === alert.alert_id}
                      onClick={() => act(alert, "acknowledge")}
                    >
                      Acknowledge
                    </Button>
                  )}
                  {alert.alert_type === "leakage_proposal" &&
                    alert.status === "pending_review" && (
                      <>
                        <Button
                          type="button"
                          size="sm"
                          loading={busy === alert.alert_id}
                          onClick={() => act(alert, "approve")}
                        >
                          Approve
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          loading={busy === alert.alert_id}
                          onClick={() => act(alert, "dismiss")}
                        >
                          Dismiss
                        </Button>
                      </>
                    )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
