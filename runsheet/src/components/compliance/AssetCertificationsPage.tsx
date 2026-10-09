"use client";

/**
 * Compliance → Certifications (UI revamp task 3.5): the summary cards are the
 * status chips' counts, one toolbar, a DataTable of assets (worst status
 * first), an asset's certifications in a Drawer, and "Add certification" as
 * an md FormDialog (design.md §5).
 */
import { Eye, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  Drawer,
  EntityLink,
  Field,
  FilterChips,
  FormDialog,
  IconButton,
  INPUT_CLASS,
  Select,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { calendarDate, number } from "../../lib/format";
import type { StatusKey } from "../../styles/tokens";
import { PageTitle } from "../ui/PageHeader";
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
import AssetPicker from "../ops/AssetPicker";

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

// ─── Status mapping ──────────────────────────────────────────────────────────

const CERT_STATUS: Record<string, { status: StatusKey; label: string }> = {
  valid: { status: "ok", label: "Valid" },
  expiring_soon: { status: "warning", label: "Expiring Soon" },
  expired: { status: "critical", label: "Expired" },
};

function CertStatusBadge({
  status,
  label,
}: {
  status: CertificationStatus;
  label?: string;
}) {
  const s = CERT_STATUS[status] ?? {
    status: "draft" as StatusKey,
    label: String(status).replace(/_/g, " "),
  };
  return <StatusBadge status={s.status} label={label ?? s.label} />;
}

function formatDate(dateStr: string | null): string {
  return dateStr ? calendarDate(dateStr) : "—";
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
    header: "Type",
    className: "font-medium text-text",
    cell: (cert) =>
      CERT_TYPE_LABELS[cert.certification_type] || cert.certification_type,
  },
  {
    key: "status",
    header: "Status",
    width: 140,
    cell: (cert) => <CertStatusBadge status={cert.status} />,
  },
  {
    key: "certification_date",
    header: "Certified",
    width: 150,
    cell: (cert) => formatDate(cert.certification_date),
  },
  {
    key: "expiry_date",
    header: "Expires",
    width: 150,
    cell: (cert) => formatDate(cert.expiry_date),
  },
  {
    key: "inspector_name",
    header: "Inspector",
    truncate: true,
    cell: (cert) => cert.inspector_name,
  },
  {
    key: "certificate_number",
    header: "Certificate",
    truncate: true,
    className: "font-mono text-xs",
    cell: (cert) => cert.certificate_number,
  },
];

const STATUS_CHIPS: { id: "" | CertificationStatus; label: string }[] = [
  { id: "", label: "All" },
  { id: "expired", label: "Expired" },
  { id: "expiring_soon", label: "Expiring soon" },
  { id: "valid", label: "Valid" },
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function AssetCertificationsPage() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [statusFilter, setStatusFilter] = useState<"" | CertificationStatus>(
    "",
  );
  const [dashboard, setDashboard] =
    useState<AssetCertificationDashboard | null>(null);
  // Asset drawer
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [assetCertifications, setAssetCertifications] = useState<
    AssetCertification[]
  >([]);
  const [assetLoading, setAssetLoading] = useState(false);
  const [assetError, setAssetError] = useState<string | null>(null);
  const [assetCertsPage, setAssetCertsPage] = useState(1);
  const [assetCertsTotalPages, setAssetCertsTotalPages] = useState(1);
  // Add dialog: null = closed; "" = no asset prefilled.
  const [adding, setAdding] = useState<string | null>(null);

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
    // `reload` refetches after a create.
  }, [reload]);

  useEffect(() => {
    fetchDashboard();
  }, [fetchDashboard]);

  const fetchAssetCertifications = useCallback(async () => {
    if (!selectedAssetId) return;
    setAssetLoading(true);
    setAssetError(null);
    try {
      const response = await getAssetCertifications({
        asset_id: selectedAssetId,
        page: assetCertsPage,
        size: 20,
      });
      setAssetCertifications(response.data ?? []);
      setAssetCertsTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setAssetError(
        err instanceof Error
          ? err.message
          : "Failed to load asset certifications",
      );
    } finally {
      setAssetLoading(false);
    }
  }, [selectedAssetId, assetCertsPage]);

  useEffect(() => {
    if (selectedAssetId) fetchAssetCertifications();
  }, [fetchAssetCertifications, selectedAssetId]);

  const openAsset = (assetId: string) => {
    setSelectedAssetId(assetId);
    setAssetCertsPage(1);
  };

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setAdding("")}
      >
        Add Certification
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  // The backend returns a flat list of per-certification rows; group them
  // into per-asset summaries (already sorted by urgency).
  const assets = useMemo(
    () => aggregateByAsset(dashboard?.assets),
    [dashboard],
  );
  const visible = statusFilter
    ? assets.filter((a) => a.overall_status === statusFilter)
    : assets;
  const counts: Record<string, number | undefined> = dashboard
    ? {
        "": assets.length,
        expired: assets.filter((a) => a.overall_status === "expired").length,
        expiring_soon: assets.filter(
          (a) => a.overall_status === "expiring_soon",
        ).length,
        valid: assets.filter((a) => a.overall_status === "valid").length,
      }
    : {};

  const dashboardColumns: Column<AssetCertificationSummary>[] = [
    {
      key: "asset_id",
      header: "Asset",
      width: 180,
      // The compliance subject (asset) is navigable to the Fleet module
      // (Req 11.3, 13.1). The dashboard list carries no resolver `links`, so
      // link optimistically on the raw asset_id.
      cell: (asset) => (
        <EntityLink
          type="asset"
          id={asset.asset_id}
          className="font-medium"
          stopPropagation
        />
      ),
    },
    {
      key: "overall_status",
      header: "Status",
      width: 150,
      cell: (asset) => <CertStatusBadge status={asset.overall_status} />,
    },
    {
      key: "next_expiry",
      header: "Next expiry",
      width: 150,
      cell: (asset) => formatDate(asset.next_expiry_date),
    },
    {
      key: "days_until_expiry",
      header: "Days left",
      align: "right",
      width: 110,
      className: "tabular-nums",
      cell: (asset) =>
        asset.days_until_next_expiry <= 0
          ? "Overdue"
          : `${number(asset.days_until_next_expiry)} days`,
    },
    {
      key: "certifications",
      header: "Certifications",
      cell: (asset) => (
        <div className="flex flex-wrap gap-1">
          {asset.certifications.map((cert) => (
            <span
              key={cert.cert_id}
              title={`${CERT_TYPE_LABELS[cert.certification_type]}: expires ${formatDate(cert.expiry_date)}`}
            >
              <CertStatusBadge
                status={cert.status}
                label={cert.certification_type.replace(/_/g, " ")}
              />
            </span>
          ))}
        </div>
      ),
    },
  ];

  const titleRow = embedded ? null : (
    <div className="flex h-11 items-center border-b border-slate-200 px-4">
      <PageTitle className="text-base font-semibold text-text">
        Fleet Certifications
      </PageTitle>
      <div className="ml-auto">{actions}</div>
    </div>
  );

  return (
    <div className="flex h-full flex-col bg-surface">
      {titleRow}
      <Toolbar
        label="Certifications"
        filters={
          <FilterChips
            label="Certification status"
            options={STATUS_CHIPS.map((c) => ({
              id: c.id || "all",
              label: c.label,
              count: counts[c.id],
              status: c.id ? CERT_STATUS[c.id].status : undefined,
            }))}
            value={statusFilter || "all"}
            onChange={(v) =>
              setStatusFilter(v === "all" ? "" : (v as CertificationStatus))
            }
          />
        }
        end={
          <>
            {dashboard && (
              <span className="sr-only">
                Valid Certifications {dashboard.total_valid}, Expiring Soon{" "}
                {dashboard.total_expiring_soon}, Expired{" "}
                {dashboard.total_expired}
              </span>
            )}
            <IconButton
              label="Refresh"
              size="sm"
              onClick={() => setReload((n) => n + 1)}
              icon={
                <RefreshCw
                  className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
                />
              }
            />
          </>
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<AssetCertificationSummary>
          ariaLabel="Fleet certification dashboard"
          columns={dashboardColumns}
          data={loading || error ? [] : visible}
          loading={loading}
          error={error ? { message: error, onRetry: fetchDashboard } : null}
          getRowId={(asset) => asset.asset_id}
          rowLabel={(asset) => `Asset ${asset.asset_id}`}
          onRowClick={(asset) => openAsset(asset.asset_id)}
          rowMenu={(asset) => [
            {
              id: "view",
              label: "View certifications",
              icon: <Eye className="h-3.5 w-3.5" />,
              onSelect: () => openAsset(asset.asset_id),
            },
            {
              id: "add",
              label: "Add certification",
              icon: <Plus className="h-3.5 w-3.5" />,
              onSelect: () => setAdding(asset.asset_id),
            },
          ]}
          emptyState={<span className="text-text-muted">No assets found.</span>}
        />
      </div>

      <Drawer
        open={selectedAssetId !== null}
        onClose={() => setSelectedAssetId(null)}
        title={
          selectedAssetId
            ? `Certifications · ${selectedAssetId}`
            : "Certifications"
        }
        width={760}
      >
        <div className="flex items-center justify-between gap-2 px-4 py-2">
          {selectedAssetId && <EntityLink type="asset" id={selectedAssetId} />}
          <Button
            size="sm"
            variant="secondary"
            icon={<Plus className="h-3.5 w-3.5" />}
            onClick={() => setAdding(selectedAssetId ?? "")}
          >
            Add certification
          </Button>
        </div>
        <DataTable<AssetCertification>
          ariaLabel={`Certifications for asset ${selectedAssetId ?? ""}`}
          columns={assetCertColumns}
          data={assetLoading || assetError ? [] : assetCertifications}
          loading={assetLoading}
          error={
            assetError
              ? { message: assetError, onRetry: fetchAssetCertifications }
              : null
          }
          getRowId={(cert) => cert.cert_id}
          pagination={
            assetCertsTotalPages > 1
              ? {
                  page: assetCertsPage,
                  totalPages: assetCertsTotalPages,
                  onPageChange: setAssetCertsPage,
                }
              : undefined
          }
          emptyState={
            <span className="text-text-muted">
              No certifications found for this asset.
            </span>
          }
        />
      </Drawer>

      {adding !== null && (
        <CertificationDialog
          prefilledAssetId={adding || null}
          onClose={() => setAdding(null)}
          onSaved={() => {
            setReload((n) => n + 1);
            if (selectedAssetId) void fetchAssetCertifications();
          }}
        />
      )}
    </div>
  );
}

// ─── Add certification dialog ────────────────────────────────────────────────

type CertValues = {
  asset_id: string;
  certification_type: CertificationType;
  certification_date: string;
  expiry_date: string;
  inspector_name: string;
  certificate_number: string;
};

export function validateCertification(v: CertValues) {
  const errors: Record<string, string | undefined> = {};
  if (!v.asset_id) errors.asset_id = "Pick an asset.";
  if (!v.certification_date)
    errors.certification_date = "Enter the certification date.";
  if (!v.expiry_date) errors.expiry_date = "Enter the expiry date.";
  else if (v.certification_date && v.expiry_date <= v.certification_date)
    errors.expiry_date = "Expiry must be after the certification date.";
  if (!v.inspector_name.trim()) errors.inspector_name = "Enter the inspector.";
  if (!v.certificate_number.trim())
    errors.certificate_number = "Enter the certificate number.";
  return errors;
}

function CertificationDialog({
  prefilledAssetId,
  onClose,
  onSaved,
}: {
  prefilledAssetId: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const submit = async (v: CertValues) => {
    const data: CreateAssetCertificationPayload = {
      asset_id: v.asset_id,
      certification_type: v.certification_type,
      certification_date: v.certification_date,
      expiry_date: v.expiry_date,
      inspector_name: v.inspector_name.trim(),
      certificate_number: v.certificate_number.trim(),
    };
    await createAssetCertification(data);
  };
  return (
    <FormDialog<CertValues, void>
      open
      size="md"
      title="Add certification"
      submitLabel="Add Certification"
      successMessage="Certification added"
      initialValues={{
        asset_id: prefilledAssetId ?? "",
        certification_type: "V_test",
        certification_date: "",
        expiry_date: "",
        inspector_name: "",
        certificate_number: "",
      }}
      validate={validateCertification}
      onSubmit={submit}
      onSaved={onSaved}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field label="Asset ID" required span={1} error={errors.asset_id}>
            {/* Cargo-tank trucks are fleet vehicles, so the roster is filtered
                to vehicle assets. */}
            <AssetPicker
              id="asset-id"
              assetType="vehicle"
              aria-label="Asset ID"
              value={values.asset_id || null}
              onChange={(value) => set("asset_id", value)}
            />
          </Field>
          <Field label="Certification Type" span={1}>
            <Select
              id="cert-type"
              value={values.certification_type}
              onChange={(v) =>
                set("certification_type", v as CertificationType)
              }
              options={Object.entries(CERT_TYPE_LABELS).map(
                ([value, label]) => ({ value, label }),
              )}
            />
          </Field>
          <Field
            label="Certification Date"
            required
            span={1}
            error={errors.certification_date}
          >
            <input
              id="cert-date"
              type="date"
              value={values.certification_date}
              onChange={(e) => set("certification_date", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
          <Field
            label="Expiry Date"
            required
            span={1}
            error={errors.expiry_date}
          >
            <input
              id="expiry-date"
              type="date"
              value={values.expiry_date}
              onChange={(e) => set("expiry_date", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
          <Field
            label="Inspector Name"
            required
            span={1}
            error={errors.inspector_name}
          >
            <input
              id="inspector-name"
              type="text"
              value={values.inspector_name}
              onChange={(e) => set("inspector_name", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
          <Field
            label="Certificate Number"
            required
            span={1}
            error={errors.certificate_number}
          >
            <input
              id="cert-number"
              type="text"
              value={values.certificate_number}
              onChange={(e) => set("certificate_number", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
        </>
      )}
    </FormDialog>
  );
}
