"use client";

import { AlertTriangle, ChevronRight, Shield, User } from "lucide-react";
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
    critical: "bg-error-light text-error-dark border-error-light",
    urgent: "bg-warning-light text-warning-dark border-warning-light",
    warning: "bg-warning-light text-warning-dark border-warning-light",
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
        if (driverRes.status === "rejected" && assetRes.status === "rejected") {
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
  const totalUrgent = (driverAlerts?.urgent ?? 0) + (assetAlerts?.urgent ?? 0);
  const totalWarning =
    (driverAlerts?.warning ?? 0) + (assetAlerts?.warning ?? 0);
  const totalAlerts = totalCritical + totalUrgent + totalWarning;

  const headerColor =
    totalCritical > 0
      ? "text-error-dark"
      : totalUrgent > 0
        ? "text-warning-dark"
        : totalWarning > 0
          ? "text-warning-dark"
          : "text-success-dark";

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
              <div className="w-2 h-2 rounded-full bg-success" />
              <span className="text-sm text-success-dark font-medium">
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
                className={`flex items-center justify-between p-2.5 rounded-lg bg-gray-50 transition-colors ${
                  onViewDrivers
                    ? "hover:bg-gray-100 cursor-pointer"
                    : "cursor-default"
                }`}
                onClick={onViewDrivers}
                role={onViewDrivers ? "button" : undefined}
                tabIndex={onViewDrivers ? 0 : undefined}
                aria-label={
                  onViewDrivers ? "View driver qualification alerts" : undefined
                }
                onKeyDown={
                  onViewDrivers
                    ? (e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onViewDrivers();
                        }
                      }
                    : undefined
                }
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
                    <ChevronRight className="w-3.5 h-3.5 text-gray-500 ml-1" />
                  )}
                </div>
              </div>
            )}

          {/* Asset Certification Alerts Section */}
          {assetAlerts &&
            (assetAlerts.critical > 0 || assetAlerts.urgent > 0) && (
              <div
                className={`flex items-center justify-between p-2.5 rounded-lg bg-gray-50 transition-colors ${
                  onViewCertifications
                    ? "hover:bg-gray-100 cursor-pointer"
                    : "cursor-default"
                }`}
                onClick={onViewCertifications}
                role={onViewCertifications ? "button" : undefined}
                tabIndex={onViewCertifications ? 0 : undefined}
                aria-label={
                  onViewCertifications
                    ? "View asset certification alerts"
                    : undefined
                }
                onKeyDown={
                  onViewCertifications
                    ? (e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onViewCertifications();
                        }
                      }
                    : undefined
                }
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
                    <ChevronRight className="w-3.5 h-3.5 text-gray-500 ml-1" />
                  )}
                </div>
              </div>
            )}
        </div>
      )}
    </div>
  );
}
