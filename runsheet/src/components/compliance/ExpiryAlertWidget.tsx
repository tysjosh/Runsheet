"use client";

import { AlertTriangle } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import {
  type AssetCertificationDashboard,
  type DQFDashboard,
  getAssetCertificationsDashboard,
  getDriversDashboard,
} from "../../services/complianceApi";

// ─── Types ───────────────────────────────────────────────────────────────────

interface AlertCounts {
  critical: number;
  urgent: number;
  warning: number;
}

interface ExpiryAlertWidgetProps {
  /** Optional callback when user clicks to view full details */
  onViewDrivers?: () => void;
  onViewCertifications?: () => void;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function countDriverAlerts(dashboard: DQFDashboard): AlertCounts {
  let critical = 0;
  let urgent = 0;
  let warning = 0;

  for (const entry of dashboard.drivers) {
    for (const qual of entry.qualifications) {
      switch (qual.alert_level) {
        case "critical":
        case "expired":
          critical++;
          break;
        case "urgent":
          urgent++;
          break;
        case "warning":
          warning++;
          break;
      }
    }
  }

  return { critical, urgent, warning };
}

function countAssetAlerts(dashboard: AssetCertificationDashboard): AlertCounts {
  return {
    critical: dashboard.total_expired,
    urgent: dashboard.total_expiring_soon,
    warning: 0, // Asset dashboard only tracks expired and expiring_soon
  };
}

// ─── Header chip ─────────────────────────────────────────────────────────────

/**
 * Compliance expiry count for Fleet's title row (UI revamp task 3.1, design
 * §6 rule 4): the former 196 px "Compliance Expiry Alerts" card is now one
 * chip that links to Compliance. It shows nothing while loading, when both
 * reads fail, or when everything is current (no static warning to scan).
 * Expired or critical items use the critical style; otherwise warning.
 * Icon + count + words, so colour is never the only signal.
 */
export default function ExpiryAlertWidget({
  onViewDrivers: _onViewDrivers,
  onViewCertifications,
  href = "/dashboard/compliance?tab=certifications",
}: ExpiryAlertWidgetProps & { href?: string }) {
  const [counts, setCounts] = useState<AlertCounts | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [driverRes, assetRes] = await Promise.allSettled([
        getDriversDashboard(),
        getAssetCertificationsDashboard(),
      ]);
      if (cancelled) return;
      if (driverRes.status === "rejected" && assetRes.status === "rejected")
        return;
      const d =
        driverRes.status === "fulfilled"
          ? countDriverAlerts(driverRes.value.data)
          : { critical: 0, urgent: 0, warning: 0 };
      const a =
        assetRes.status === "fulfilled"
          ? countAssetAlerts(assetRes.value.data)
          : { critical: 0, urgent: 0, warning: 0 };
      setCounts({
        critical: d.critical + a.critical,
        urgent: d.urgent + a.urgent,
        warning: d.warning + a.warning,
      });
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!counts) return null;
  const total = counts.critical + counts.urgent + counts.warning;
  if (total === 0) return null;
  const critical = counts.critical > 0;
  const label = critical
    ? `${counts.critical} expired · ${total} compliance alerts`
    : `${total} expiring soon`;
  const cls = `inline-flex h-6 shrink-0 items-center gap-1 whitespace-nowrap rounded-full border px-2 text-xs font-semibold focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
    critical
      ? "border-red-300 bg-red-50 text-red-800 hover:bg-red-100"
      : "border-amber-300 bg-amber-50 text-amber-900 hover:bg-amber-100"
  }`;
  const body = (
    <>
      <AlertTriangle aria-hidden="true" className="h-3 w-3" />
      {label}
    </>
  );
  return onViewCertifications ? (
    <button
      type="button"
      className={cls}
      onClick={onViewCertifications}
      data-testid="expiry-chip"
    >
      {body}
    </button>
  ) : (
    <Link href={href} className={cls} data-testid="expiry-chip">
      {body}
    </Link>
  );
}
