"use client";

import React, { useEffect, useState } from "react";
import { AlertTriangle, Shield, User, ChevronRight } from "lucide-react";
import {
  getDriversDashboard,
  getAssetCertificationsDashboard,
  type DQFDashboard,
  type AssetCertificationDashboard,
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

// ─── Badge Component ─────────────────────────────────────────────────────────

function AlertBadge({
  count,
  level,
}: {
  count: number;
  level: "critical" | "urgent" | "warning";
}) {
  if (count === 0) return null;

  const styles = {
    critical: "bg-red-100 text-red-800 border-red-200",
    urgent: "bg-orange-100 text-orange-800 border-orange-200",
    warning: "bg-yellow-100 text-yellow-800 border-yellow-200",
  };

  const labels = {
    critical: "Critical",
    urgent: "Urgent",
    warning: "Warning",
  };

  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full border ${styles[level]}`}
    >
      {count} {labels[level]}
    </span>
  );
}

// ─── Main Widget ─────────────────────────────────────────────────────────────

export default function ExpiryAlertWidget({
  onViewDrivers,
  onViewCertifications,
}: ExpiryAlertWidgetProps) {
  const [driverAlerts, setDriverAlerts] = useState<AlertCounts | null>(null);
  const [assetAlerts, setAssetAlerts] = useState<AlertCounts | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function fetchAlerts() {
      setLoading(true);
      setError(null);

      try {
        const [driverRes, assetRes] = await Promise.allSettled([
          getDriversDashboard(),
          getAssetCertificationsDashboard(),
        ]);

        if (cancelled) return;

        if (driverRes.status === "fulfilled") {
          setDriverAlerts(countDriverAlerts(driverRes.value.data));
        }

        if (assetRes.status === "fulfilled") {
          setAssetAlerts(countAssetAlerts(assetRes.value.data));
        }

        // Only show error if both failed
        if (
          driverRes.status === "rejected" &&
          assetRes.status === "rejected"
        ) {
          setError("Unable to load compliance alerts");
        }
      } catch {
        if (!cancelled) {
          setError("Unable to load compliance alerts");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    fetchAlerts();

    return () => {
      cancelled = true;
    };
  }, []);

  const totalCritical =
    (driverAlerts?.critical ?? 0) + (assetAlerts?.critical ?? 0);
  const totalUrgent =
    (driverAlerts?.urgent ?? 0) + (assetAlerts?.urgent ?? 0);
  const totalWarning =
    (driverAlerts?.warning ?? 0) + (assetAlerts?.warning ?? 0);
  const totalAlerts = totalCritical + totalUrgent + totalWarning;

  const headerColor =
    totalCritical > 0
      ? "text-red-700"
      : totalUrgent > 0
        ? "text-orange-700"
        : totalWarning > 0
          ? "text-yellow-700"
          : "text-green-700";

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-4">
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <AlertTriangle className={`w-4 h-4 ${headerColor}`} />
        <h3 className="text-sm font-semibold text-gray-900">
          Compliance Expiry Alerts
        </h3>
        {!loading && !error && totalAlerts > 0 && (
          <span className="ml-auto text-xs font-medium text-gray-500">
            {totalAlerts} alert{totalAlerts !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {/* Loading state */}
      {loading && (
        <div className="flex items-center justify-center py-4">
          <div className="w-5 h-5 border-2 border-gray-300 border-t-gray-600 rounded-full animate-spin" />
          <span className="ml-2 text-xs text-gray-500">Loading alerts...</span>
        </div>
      )}

      {/* Error state */}
      {!loading && error && (
        <p className="text-xs text-gray-500 py-2">{error}</p>
      )}

      {/* Content */}
      {!loading && !error && (
        <div className="space-y-3">
          {/* All clear state */}
          {totalAlerts === 0 && (
            <div className="flex items-center gap-2 py-2">
              <div className="w-2 h-2 rounded-full bg-green-500" />
              <span className="text-sm text-green-700 font-medium">
                All qualifications and certifications current
              </span>
            </div>
          )}

          {/* Driver Alerts Section */}
          {driverAlerts &&
            (driverAlerts.critical > 0 ||
              driverAlerts.urgent > 0 ||
              driverAlerts.warning > 0) && (
              <div
                className="flex items-center justify-between p-2.5 rounded-lg bg-gray-50 hover:bg-gray-100 transition-colors cursor-pointer"
                onClick={onViewDrivers}
                role="button"
                tabIndex={0}
                aria-label="View driver qualification alerts"
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onViewDrivers?.();
                  }
                }}
              >
                <div className="flex items-center gap-2">
                  <User className="w-4 h-4 text-gray-500" />
                  <span className="text-sm font-medium text-gray-700">
                    Driver Qualifications
                  </span>
                </div>
                <div className="flex items-center gap-1.5">
                  <AlertBadge count={driverAlerts.critical} level="critical" />
                  <AlertBadge count={driverAlerts.urgent} level="urgent" />
                  <AlertBadge count={driverAlerts.warning} level="warning" />
                  {onViewDrivers && (
                    <ChevronRight className="w-3.5 h-3.5 text-gray-400 ml-1" />
                  )}
                </div>
              </div>
            )}

          {/* Asset Certification Alerts Section */}
          {assetAlerts &&
            (assetAlerts.critical > 0 || assetAlerts.urgent > 0) && (
              <div
                className="flex items-center justify-between p-2.5 rounded-lg bg-gray-50 hover:bg-gray-100 transition-colors cursor-pointer"
                onClick={onViewCertifications}
                role="button"
                tabIndex={0}
                aria-label="View asset certification alerts"
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onViewCertifications?.();
                  }
                }}
              >
                <div className="flex items-center gap-2">
                  <Shield className="w-4 h-4 text-gray-500" />
                  <span className="text-sm font-medium text-gray-700">
                    Asset Certifications
                  </span>
                </div>
                <div className="flex items-center gap-1.5">
                  <AlertBadge count={assetAlerts.critical} level="critical" />
                  <AlertBadge count={assetAlerts.urgent} level="urgent" />
                  {onViewCertifications && (
                    <ChevronRight className="w-3.5 h-3.5 text-gray-400 ml-1" />
                  )}
                </div>
              </div>
            )}
        </div>
      )}
    </div>
  );
}
