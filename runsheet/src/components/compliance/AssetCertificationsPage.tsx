"use client";

import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  Badge,
  type BadgeVariant,
  Button,
  type Column,
  PageHeader,
  Pagination,
  Table,
} from "@/components/ui";
import {
  type AssetCertification,
  type AssetCertificationDashboard,
  type CertificationStatus,
  type CertificationType,
  type CreateAssetCertificationPayload,
  createAssetCertification,
  type FleetCertificationEntry,
  getAssetCertifications,
  getAssetCertificationsDashboard,
} from "../../services/complianceApi";

// ─── Sub-view types ──────────────────────────────────────────────────────────

type ViewMode = "dashboard" | "asset-detail" | "add";

// ─── Certification type labels ───────────────────────────────────────────────

const CERT_TYPE_LABELS: Record<CertificationType, string> = {
  V_test: "V Test (Visual)",
  K_test: "K Test (Thickness)",
  I_test: "I Test (Internal)",
  P_test: "P Test (Pressure)",
  UT_test: "UT Test (Ultrasonic)",
  meter_seal: "Meter Seal",
  fire_extinguisher: "Fire Extinguisher",
};

// ─── Status color mapping ────────────────────────────────────────────────────

function certStatusVariant(status: CertificationStatus): BadgeVariant {
  switch (status) {
    case "valid":
      return "success";
    case "expiring_soon":
      return "warning";
    case "expired":
      return "error";
    default:
      return "neutral";
  }
}

function certStatusLabel(status: CertificationStatus) {
  switch (status) {
    case "valid":
      return "Valid";
    case "expiring_soon":
      return "Expiring Soon";
    case "expired":
      return "Expired";
    default:
      return status;
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString();
}

// ─── Urgency sort helper ─────────────────────────────────────────────────────

function urgencyOrder(status: CertificationStatus): number {
  switch (status) {
    case "expired":
      return 0;
    case "expiring_soon":
      return 1;
    case "valid":
      return 2;
    default:
      return 3;
  }
}

// ─── Per-asset aggregation ───────────────────────────────────────────────────

/**
 * A per-asset rollup of the backend's flat per-certification dashboard
 * rows. The backend returns one {@link FleetCertificationEntry} per
 * certification; the table groups them by ``asset_id`` so each asset is a
 * single row with its certifications, worst-case status, and soonest
 * expiry.
 */
interface AssetCertificationSummary {
  asset_id: string;
  certifications: FleetCertificationEntry[];
  overall_status: CertificationStatus;
  next_expiry_date: string | null;
  days_until_next_expiry: number;
}

/** Worst (most urgent) of two statuses wins for the asset rollup. */
function worstStatus(
  a: CertificationStatus,
  b: CertificationStatus,
): CertificationStatus {
  return urgencyOrder(a) <= urgencyOrder(b) ? a : b;
}

/**
 * Group the backend's flat certification list into per-asset summaries,
 * sorted by urgency (most urgent first). Defensive against a missing or
 * non-array ``assets`` payload so a malformed response degrades to an
 * empty table rather than crashing the dashboard.
 */
function aggregateByAsset(
  entries: FleetCertificationEntry[] | undefined | null,
): AssetCertificationSummary[] {
  const byAsset = new Map<string, AssetCertificationSummary>();

  for (const entry of entries ?? []) {
    const existing = byAsset.get(entry.asset_id);
    if (!existing) {
      byAsset.set(entry.asset_id, {
        asset_id: entry.asset_id,
        certifications: [entry],
        overall_status: entry.status,
        next_expiry_date: entry.expiry_date,
        days_until_next_expiry: entry.days_until_expiry,
      });
      continue;
    }

    existing.certifications.push(entry);
    existing.overall_status = worstStatus(
      existing.overall_status,
      entry.status,
    );
    if (entry.days_until_expiry < existing.days_until_next_expiry) {
      existing.days_until_next_expiry = entry.days_until_expiry;
      existing.next_expiry_date = entry.expiry_date;
    }
  }

  return [...byAsset.values()].sort(
    (a, b) =>
      urgencyOrder(a.overall_status) - urgencyOrder(b.overall_status) ||
      a.days_until_next_expiry - b.days_until_next_expiry,
  );
}

// ─── Table columns ───────────────────────────────────────────────────────────

const assetCertColumns: Column<AssetCertification>[] = [
  {
    key: "certification_type",
    label: "Type",
    render: (cert) => (
      <span className="font-medium">
        {CERT_TYPE_LABELS[cert.certification_type] || cert.certification_type}
      </span>
    ),
  },
  {
    key: "status",
    label: "Status",
    render: (cert) => (
      <Badge variant={certStatusVariant(cert.status)} size="sm">
        {certStatusLabel(cert.status)}
      </Badge>
    ),
  },
  {
    key: "certification_date",
    label: "Certification Date",
    render: (cert) => formatDate(cert.certification_date),
  },
  {
    key: "expiry_date",
    label: "Expiry Date",
    render: (cert) => formatDate(cert.expiry_date),
  },
  {
    key: "inspector_name",
    label: "Inspector",
    render: (cert) => cert.inspector_name,
  },
  {
    key: "certificate_number",
    label: "Certificate #",
    render: (cert) => cert.certificate_number,
  },
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function AssetCertificationsPage() {
  const [viewMode, setViewMode] = useState<ViewMode>("dashboard");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Dashboard state
  const [dashboard, setDashboard] =
    useState<AssetCertificationDashboard | null>(null);

  // Asset detail state
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [assetCertifications, setAssetCertifications] = useState<
    AssetCertification[]
  >([]);
  const [assetCertsPage, setAssetCertsPage] = useState(1);
  const [assetCertsTotalPages, setAssetCertsTotalPages] = useState(1);

  // ─── Fetch dashboard ─────────────────────────────────────────────────────

  const fetchDashboard = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getAssetCertificationsDashboard();
      setDashboard(response.data);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load fleet certification dashboard",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (viewMode === "dashboard") {
      fetchDashboard();
    }
  }, [fetchDashboard, viewMode]);

  // ─── Fetch certifications for a specific asset ───────────────────────────

  const fetchAssetCertifications = useCallback(async () => {
    if (!selectedAssetId) return;
    setLoading(true);
    setError(null);
    try {
      const response = await getAssetCertifications({
        asset_id: selectedAssetId,
        page: assetCertsPage,
        size: 20,
      });
      setAssetCertifications(response.data ?? []);
      setAssetCertsTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load asset certifications",
      );
    } finally {
      setLoading(false);
    }
  }, [selectedAssetId, assetCertsPage]);

  useEffect(() => {
    if (viewMode === "asset-detail" && selectedAssetId) {
      fetchAssetCertifications();
    }
  }, [fetchAssetCertifications, viewMode, selectedAssetId]);

  // ─── Handlers ────────────────────────────────────────────────────────────

  const handleViewAsset = (assetId: string) => {
    setSelectedAssetId(assetId);
    setAssetCertsPage(1);
    setViewMode("asset-detail");
  };

  const handleBackToDashboard = () => {
    setSelectedAssetId(null);
    setAssetCertifications([]);
    setViewMode("dashboard");
  };

  // ─── Render: Dashboard Summary Cards ─────────────────────────────────────

  function renderSummaryCards() {
    if (!dashboard) return null;
    return (
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
          <p className="text-sm text-gray-500">Valid Certifications</p>
          <p className="text-2xl font-bold text-success-dark">
            {dashboard.total_valid}
          </p>
        </div>
        <div className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
          <p className="text-sm text-gray-500">Expiring Soon</p>
          <p className="text-2xl font-bold text-warning-dark">
            {dashboard.total_expiring_soon}
          </p>
        </div>
        <div className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
          <p className="text-sm text-gray-500">Expired</p>
          <p className="text-2xl font-bold text-error-dark">
            {dashboard.total_expired}
          </p>
        </div>
      </div>
    );
  }

  // ─── Render: Dashboard Fleet Table ───────────────────────────────────────

  function renderDashboard() {
    if (loading) {
      return (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">
            Loading fleet certification dashboard...
          </span>
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      );
    }

    if (!dashboard) return null;

    // The backend returns a flat list of per-certification rows; group
    // them into per-asset summaries (already sorted by urgency).
    const sortedAssets = aggregateByAsset(dashboard.assets);

    const dashboardColumns: Column<AssetCertificationSummary>[] = [
      {
        key: "asset_id",
        label: "Asset",
        render: (asset) => (
          <span className="font-medium">{asset.asset_id}</span>
        ),
      },
      {
        key: "overall_status",
        label: "Overall Status",
        render: (asset) => (
          <Badge variant={certStatusVariant(asset.overall_status)} size="sm">
            {certStatusLabel(asset.overall_status)}
          </Badge>
        ),
      },
      {
        key: "next_expiry",
        label: "Next Expiry",
        render: (asset) => formatDate(asset.next_expiry_date),
      },
      {
        key: "days_until_expiry",
        label: "Days Until Expiry",
        render: (asset) => (
          <span
            className={`font-medium ${
              asset.days_until_next_expiry <= 7
                ? "text-error-dark"
                : asset.days_until_next_expiry <= 30
                  ? "text-warning-dark"
                  : "text-gray-700"
            }`}
          >
            {asset.days_until_next_expiry <= 0
              ? "Overdue"
              : `${asset.days_until_next_expiry} days`}
          </span>
        ),
      },
      {
        key: "certifications",
        label: "Certifications",
        render: (asset) => (
          <div className="flex flex-wrap gap-1">
            {asset.certifications.map((cert) => (
              <span
                key={cert.cert_id}
                title={`${CERT_TYPE_LABELS[cert.certification_type]}: expires ${formatDate(cert.expiry_date)}`}
              >
                <Badge variant={certStatusVariant(cert.status)} size="sm">
                  {cert.certification_type.replace(/_/g, " ")}
                </Badge>
              </span>
            ))}
          </div>
        ),
      },
      {
        key: "actions",
        label: "Actions",
        render: (asset) => (
          <Button
            type="button"
            onClick={() => handleViewAsset(asset.asset_id)}
            variant="ghost"
            size="sm"
          >
            View Details
          </Button>
        ),
      },
    ];

    return (
      <div>
        {renderSummaryCards()}

        <Table<AssetCertificationSummary>
          ariaLabel="Fleet certification dashboard"
          columns={dashboardColumns}
          data={sortedAssets}
          getRowId={(asset) => asset.asset_id}
          emptyState={<span className="text-gray-500">No assets found.</span>}
        />
      </div>
    );
  }

  // ─── Render: Asset Detail View ───────────────────────────────────────────

  function renderAssetDetail() {
    if (loading) {
      return (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading certifications...</span>
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      );
    }

    return (
      <div>
        <div className="mb-4">
          <h2 className="text-lg font-bold">
            Certifications for Asset: {selectedAssetId}
          </h2>
        </div>

        <Table<AssetCertification>
          ariaLabel={`Certifications for asset ${selectedAssetId ?? ""}`}
          columns={assetCertColumns}
          data={assetCertifications}
          getRowId={(cert) => cert.cert_id}
          emptyState={
            <span className="text-gray-500">
              No certifications found for this asset.
            </span>
          }
        />

        {/* Pagination */}
        {assetCertifications.length > 0 && (
          <Pagination
            currentPage={assetCertsPage}
            totalPages={assetCertsTotalPages}
            onPageChange={setAssetCertsPage}
            className="px-0 mt-4"
          />
        )}
      </div>
    );
  }

  // ─── Render: Add Certification Form ──────────────────────────────────────

  function renderAddForm() {
    return (
      <CertificationForm
        prefilledAssetId={selectedAssetId}
        onSubmit={async (data) => {
          setLoading(true);
          setError(null);
          try {
            await createAssetCertification(data);
            // Return to dashboard after successful creation
            setViewMode("dashboard");
          } catch (err) {
            setError(
              err instanceof Error
                ? err.message
                : "Failed to create certification",
            );
          } finally {
            setLoading(false);
          }
        }}
        onCancel={() => {
          if (selectedAssetId) {
            setViewMode("asset-detail");
          } else {
            setViewMode("dashboard");
          }
        }}
        loading={loading}
      />
    );
  }

  // ─── Main Render ─────────────────────────────────────────────────────────

  return (
    <div className="h-full flex flex-col bg-white">
      <PageHeader
        title="Fleet Certifications"
        subtitle="Track DOT cargo tank inspections, meter seals, and fire extinguisher certifications."
        actions={
          <>
            {viewMode === "asset-detail" && (
              <Button
                type="button"
                variant="secondary"
                onClick={handleBackToDashboard}
              >
                Back to Dashboard
              </Button>
            )}
            {viewMode === "add" && (
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  if (selectedAssetId) {
                    setViewMode("asset-detail");
                  } else {
                    setViewMode("dashboard");
                  }
                }}
              >
                Cancel
              </Button>
            )}
            {(viewMode === "dashboard" || viewMode === "asset-detail") && (
              <Button type="button" onClick={() => setViewMode("add")}>
                Add Certification
              </Button>
            )}
          </>
        }
      />

      {/* Error state */}
      {error && (
        <div
          role="alert"
          className="mx-8 mt-6 bg-error-light border border-error/20 text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* View content */}
      <div className="flex-1 overflow-auto px-8 py-6">
        {viewMode === "dashboard" && renderDashboard()}
        {viewMode === "asset-detail" && renderAssetDetail()}
        {viewMode === "add" && renderAddForm()}
      </div>
    </div>
  );
}

// ─── Certification Form Sub-Component ────────────────────────────────────────

interface CertificationFormProps {
  prefilledAssetId: string | null;
  onSubmit: (data: CreateAssetCertificationPayload) => Promise<void>;
  onCancel: () => void;
  loading: boolean;
}

function CertificationForm({
  prefilledAssetId,
  onSubmit,
  onCancel,
  loading,
}: CertificationFormProps) {
  const [assetId, setAssetId] = useState(prefilledAssetId ?? "");
  const [certificationType, setCertificationType] =
    useState<CertificationType>("V_test");
  const [certificationDate, setCertificationDate] = useState("");
  const [expiryDate, setExpiryDate] = useState("");
  const [inspectorName, setInspectorName] = useState("");
  const [certificateNumber, setCertificateNumber] = useState("");

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const data: CreateAssetCertificationPayload = {
      asset_id: assetId,
      certification_type: certificationType,
      certification_date: certificationDate,
      expiry_date: expiryDate,
      inspector_name: inspectorName,
      certificate_number: certificateNumber,
    };
    await onSubmit(data);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white border border-gray-200 rounded-lg p-6 shadow-sm max-w-2xl"
    >
      <h2 className="text-lg font-bold mb-4">Add New Certification</h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label htmlFor="asset-id" className="block text-sm font-medium mb-1">
            Asset ID
          </label>
          <input
            id="asset-id"
            type="text"
            required
            value={assetId}
            onChange={(e) => setAssetId(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g., TRUCK-001"
          />
        </div>
        <div>
          <label htmlFor="cert-type" className="block text-sm font-medium mb-1">
            Certification Type
          </label>
          <select
            id="cert-type"
            value={certificationType}
            onChange={(e) =>
              setCertificationType(e.target.value as CertificationType)
            }
            className="w-full border rounded px-3 py-2"
          >
            {Object.entries(CERT_TYPE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="cert-date" className="block text-sm font-medium mb-1">
            Certification Date
          </label>
          <input
            id="cert-date"
            type="date"
            required
            value={certificationDate}
            onChange={(e) => setCertificationDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
        <div>
          <label
            htmlFor="expiry-date"
            className="block text-sm font-medium mb-1"
          >
            Expiry Date
          </label>
          <input
            id="expiry-date"
            type="date"
            required
            value={expiryDate}
            onChange={(e) => setExpiryDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
        <div>
          <label
            htmlFor="inspector-name"
            className="block text-sm font-medium mb-1"
          >
            Inspector Name
          </label>
          <input
            id="inspector-name"
            type="text"
            required
            value={inspectorName}
            onChange={(e) => setInspectorName(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
        <div>
          <label
            htmlFor="cert-number"
            className="block text-sm font-medium mb-1"
          >
            Certificate Number
          </label>
          <input
            id="cert-number"
            type="text"
            required
            value={certificateNumber}
            onChange={(e) => setCertificateNumber(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
      </div>

      <div className="flex gap-3 mt-6">
        <Button type="submit" disabled={loading} loading={loading}>
          {loading ? "Saving..." : "Add Certification"}
        </Button>
        <Button type="button" onClick={onCancel} variant="secondary">
          Cancel
        </Button>
      </div>
    </form>
  );
}
