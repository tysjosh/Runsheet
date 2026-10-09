"use client";

/**
 * Compliance → Meters (UI revamp task 3.5): one toolbar (truck filter in the
 * Filters popover, refresh), a DataTable of registered meters with a
 * calibration StatusBadge, a meter's delivery audit trail in a Drawer, and
 * "Register meter" as an md FormDialog (design.md §5).
 */
import { History, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  Drawer,
  EntityLink,
  Field,
  FilterPopover,
  FormDialog,
  IconButton,
  INPUT_CLASS,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { calendarDate, dateTime, gallons, humanize } from "../../lib/format";
import {
  type CreateMeterPayload,
  createMeter,
  getMeterAuditTrail,
  getMeters,
  type MeterAuditEntry,
  type MeterRegistration,
} from "../../services/complianceApi";
import AssetPicker from "../ops/AssetPicker";
import { PageTitle } from "../ui/PageHeader";

// ─── Helpers ─────────────────────────────────────────────────────────────────

const DAY_MS = 86_400_000;

/** Calibration status from the expiry date (calendar day, UTC). */
export function calibrationStatus(
  expiryDate: string,
  now: Date = new Date(),
): { status: "ok" | "warning" | "critical"; label: string } {
  const expiry = new Date(
    /^\d{4}-\d{2}-\d{2}$/.test(expiryDate)
      ? `${expiryDate}T23:59:59Z`
      : expiryDate,
  );
  const diffMs = expiry.getTime() - now.getTime();
  if (diffMs < 0) return { status: "critical", label: "Expired" };
  const diffDays = Math.ceil(diffMs / DAY_MS);
  if (diffDays <= 30)
    return { status: "warning", label: `Expiring (${diffDays}d)` };
  return { status: "ok", label: "Valid" };
}

function formatDate(dateStr: string | null): string {
  return dateStr ? calendarDate(dateStr) : "—";
}

// ─── Columns ─────────────────────────────────────────────────────────────────

const meterColumns: Column<MeterRegistration>[] = [
  {
    key: "meter_number",
    header: "Meter",
    width: 150,
    className: "font-medium text-text",
    cell: (meter) => meter.meter_number,
  },
  {
    key: "truck_id",
    header: "Truck",
    width: 160,
    // The meter's subject is its truck, navigable to the Fleet module as a
    // canonical asset (Req 11.3, 13.1).
    cell: (meter) => (
      <EntityLink type="asset" id={meter.truck_id} stopPropagation />
    ),
  },
  {
    key: "status",
    header: "Calibration",
    width: 150,
    cell: (meter) => {
      const s = calibrationStatus(meter.calibration_expiry_date);
      return <StatusBadge status={s.status} label={s.label} />;
    },
  },
  {
    key: "calibration_expiry_date",
    header: "Expires",
    width: 150,
    cell: (meter) => formatDate(meter.calibration_expiry_date),
  },
  {
    key: "calibration_certificate_number",
    header: "Certificate",
    truncate: true,
    className: "font-mono text-xs",
    cell: (meter) => meter.calibration_certificate_number,
  },
  {
    key: "weights_measures_authority",
    header: "W&M authority",
    truncate: true,
    title: (meter) => meter.weights_measures_authority,
    cell: (meter) => meter.weights_measures_authority,
  },
];

const auditTrailColumns: Column<MeterAuditEntry>[] = [
  {
    key: "timestamp",
    header: "Time",
    width: 160,
    cell: (entry) => dateTime(entry.timestamp),
  },
  {
    key: "delivery_id",
    header: "Delivery",
    truncate: true,
    className: "font-medium",
    cell: (entry) => entry.delivery_id,
  },
  {
    key: "invoice_id",
    header: "Invoice",
    truncate: true,
    cell: (entry) => entry.invoice_id,
  },
  {
    key: "gross_gallons",
    header: "Gross",
    align: "right",
    width: 110,
    className: "tabular-nums",
    cell: (entry) => gallons(entry.gross_gallons, { decimals: 1 }),
  },
  {
    key: "net_gallons",
    header: "Net",
    align: "right",
    width: 110,
    className: "tabular-nums",
    cell: (entry) => gallons(entry.net_gallons, { decimals: 1 }),
  },
  {
    key: "variance",
    header: "Variance",
    width: 150,
    cell: (entry) =>
      entry.variance_flag ? (
        <StatusBadge status="critical" label={humanize(entry.variance_flag)} />
      ) : (
        <StatusBadge status="ok" label="OK" />
      ),
  },
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function MeterAuditPage() {
  const [meters, setMeters] = useState<MeterRegistration[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [reload, setReload] = useState(0);
  const [truckIdFilter, setTruckIdFilter] = useState<string>("");
  const [registering, setRegistering] = useState(false);

  const [selectedMeter, setSelectedMeter] = useState<MeterRegistration | null>(
    null,
  );
  const [auditEntries, setAuditEntries] = useState<MeterAuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditPage, setAuditPage] = useState(1);
  const [auditTotalPages, setAuditTotalPages] = useState(1);

  const fetchMeters = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: { truck_id?: string; page: number; size: number } = {
        page,
        size: 20,
      };
      if (truckIdFilter) filters.truck_id = truckIdFilter;
      const response = await getMeters(filters);
      setMeters(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load meters");
    } finally {
      setLoading(false);
    }
    // `reload` refetches after a registration.
  }, [page, truckIdFilter, reload]);

  useEffect(() => {
    fetchMeters();
  }, [fetchMeters]);

  const fetchAuditTrail = useCallback(async () => {
    if (!selectedMeter) return;
    setAuditLoading(true);
    setAuditError(null);
    try {
      const response = await getMeterAuditTrail(selectedMeter.meter_id, {
        page: auditPage,
        size: 20,
      });
      setAuditEntries(response.data ?? []);
      setAuditTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setAuditError(
        err instanceof Error ? err.message : "Failed to load audit trail",
      );
    } finally {
      setAuditLoading(false);
    }
  }, [selectedMeter, auditPage]);

  useEffect(() => {
    if (selectedMeter) fetchAuditTrail();
  }, [fetchAuditTrail, selectedMeter]);

  const openAudit = (meter: MeterRegistration) => {
    setSelectedMeter(meter);
    setAuditPage(1);
    setAuditEntries([]);
  };

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setRegistering(true)}
      >
        Register meter
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  const selectedStatus = selectedMeter
    ? calibrationStatus(selectedMeter.calibration_expiry_date)
    : null;

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Meter Registry & Audit
          </PageTitle>
          <div className="ml-auto">{actions}</div>
        </div>
      )}
      <Toolbar
        label="Meters"
        filters={
          <FilterPopover
            count={truckIdFilter ? 1 : 0}
            label="Meter filters"
            onClear={() => {
              setTruckIdFilter("");
              setPage(1);
            }}
          >
            <Field label="Truck" id="truck-id-filter">
              {/* Filter by a real fleet vehicle; blank (cleared) = all. */}
              <AssetPicker
                id="truck-id-filter"
                assetType="vehicle"
                aria-label="Truck ID"
                value={truckIdFilter || null}
                onChange={(value) => {
                  setTruckIdFilter(value);
                  setPage(1);
                }}
                allowClear
              />
            </Field>
          </FilterPopover>
        }
        end={
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
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<MeterRegistration>
          ariaLabel="Registered meters"
          columns={meterColumns}
          data={loading || error ? [] : meters}
          loading={loading}
          error={error ? { message: error, onRetry: fetchMeters } : null}
          getRowId={(meter) => meter.meter_id}
          rowLabel={(meter) => `Meter ${meter.meter_number}`}
          onRowClick={openAudit}
          rowMenu={(meter) => [
            {
              id: "audit",
              label: "Audit trail",
              icon: <History className="h-3.5 w-3.5" />,
              onSelect: () => openAudit(meter),
            },
          ]}
          pagination={
            totalPages > 1
              ? { page, totalPages, onPageChange: setPage }
              : undefined
          }
          emptyState={
            <span className="text-text-muted">No meters registered.</span>
          }
        />
      </div>

      <Drawer
        open={selectedMeter !== null}
        onClose={() => setSelectedMeter(null)}
        title={
          selectedMeter
            ? `Audit trail · ${selectedMeter.meter_number}`
            : "Audit trail"
        }
        width={820}
      >
        {selectedMeter && selectedStatus && (
          <dl className="grid grid-cols-2 gap-3 border-b border-slate-200 px-4 py-3 text-sm md:grid-cols-4">
            <div>
              <dt className="text-xs text-text-muted">Meter</dt>
              <dd className="font-semibold">{selectedMeter.meter_number}</dd>
            </div>
            <div>
              <dt className="text-xs text-text-muted">Truck</dt>
              <dd>
                <EntityLink type="asset" id={selectedMeter.truck_id} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-text-muted">Calibration expires</dt>
              <dd>{formatDate(selectedMeter.calibration_expiry_date)}</dd>
            </div>
            <div>
              <dt className="text-xs text-text-muted">Status</dt>
              <dd>
                <StatusBadge
                  status={selectedStatus.status}
                  label={selectedStatus.label}
                />
              </dd>
            </div>
          </dl>
        )}
        <DataTable<MeterAuditEntry>
          ariaLabel="Meter delivery audit trail"
          columns={auditTrailColumns}
          data={auditLoading || auditError ? [] : auditEntries}
          loading={auditLoading}
          error={
            auditError
              ? { message: auditError, onRetry: fetchAuditTrail }
              : null
          }
          getRowId={(entry) => entry.audit_id}
          pagination={
            auditTotalPages > 1
              ? {
                  page: auditPage,
                  totalPages: auditTotalPages,
                  onPageChange: setAuditPage,
                }
              : undefined
          }
          emptyState={
            <span className="text-text-muted">
              No audit entries found for this meter.
            </span>
          }
        />
      </Drawer>

      {registering && (
        <RegisterMeterDialog
          onClose={() => setRegistering(false)}
          onSaved={() => setReload((n) => n + 1)}
        />
      )}
    </div>
  );
}

// ─── Register meter dialog ───────────────────────────────────────────────────

type MeterValues = {
  meter_number: string;
  truck_id: string;
  calibration_certificate_number: string;
  weights_measures_authority: string;
  calibration_date: string;
  calibration_expiry_date: string;
};

export function validateMeter(v: MeterValues) {
  const errors: Record<string, string | undefined> = {};
  if (!v.meter_number.trim()) errors.meter_number = "Enter the meter number.";
  if (!v.truck_id) errors.truck_id = "Pick a truck.";
  if (!v.calibration_certificate_number.trim())
    errors.calibration_certificate_number = "Enter the certificate number.";
  if (!v.weights_measures_authority.trim())
    errors.weights_measures_authority = "Enter the authority.";
  if (!v.calibration_date)
    errors.calibration_date = "Enter the calibration date.";
  if (!v.calibration_expiry_date)
    errors.calibration_expiry_date = "Enter the expiry date.";
  else if (
    v.calibration_date &&
    v.calibration_expiry_date <= v.calibration_date
  )
    errors.calibration_expiry_date =
      "Expiry must be after the calibration date.";
  return errors;
}

function RegisterMeterDialog({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const submit = async (v: MeterValues) => {
    const data: CreateMeterPayload = {
      meter_number: v.meter_number.trim(),
      truck_id: v.truck_id,
      calibration_certificate_number: v.calibration_certificate_number.trim(),
      calibration_date: v.calibration_date,
      calibration_expiry_date: v.calibration_expiry_date,
      weights_measures_authority: v.weights_measures_authority.trim(),
    };
    await createMeter(data);
  };
  return (
    <FormDialog<MeterValues, void>
      open
      size="md"
      title="Register meter"
      submitLabel="Register meter"
      successMessage="Meter registered"
      initialValues={{
        meter_number: "",
        truck_id: "",
        calibration_certificate_number: "",
        weights_measures_authority: "",
        calibration_date: "",
        calibration_expiry_date: "",
      }}
      validate={validateMeter}
      onSubmit={submit}
      onSaved={onSaved}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field
            label="Meter number"
            required
            span={1}
            error={errors.meter_number}
          >
            <input
              id="meter-number"
              type="text"
              value={values.meter_number}
              onChange={(e) => set("meter_number", e.target.value)}
              className={INPUT_CLASS}
              placeholder="e.g. MTR-001"
            />
          </Field>
          <Field label="Truck" required span={1} error={errors.truck_id}>
            <AssetPicker
              id="meter-truck-id"
              assetType="vehicle"
              aria-label="Truck ID"
              value={values.truck_id || null}
              onChange={(value) => set("truck_id", value)}
            />
          </Field>
          <Field
            label="Calibration certificate"
            required
            span={1}
            error={errors.calibration_certificate_number}
          >
            <input
              id="meter-cert-number"
              type="text"
              value={values.calibration_certificate_number}
              onChange={(e) =>
                set("calibration_certificate_number", e.target.value)
              }
              className={INPUT_CLASS}
              placeholder="e.g. CAL-2024-0001"
            />
          </Field>
          <Field
            label="Weights & Measures authority"
            required
            span={1}
            error={errors.weights_measures_authority}
          >
            <input
              id="meter-wm-authority"
              type="text"
              value={values.weights_measures_authority}
              onChange={(e) =>
                set("weights_measures_authority", e.target.value)
              }
              className={INPUT_CLASS}
              placeholder="e.g. TX Dept of Agriculture"
            />
          </Field>
          <Field
            label="Calibration date"
            required
            span={1}
            error={errors.calibration_date}
          >
            <input
              id="meter-calibration-date"
              type="date"
              value={values.calibration_date}
              onChange={(e) => set("calibration_date", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
          <Field
            label="Calibration expiry"
            required
            span={1}
            error={errors.calibration_expiry_date}
          >
            <input
              id="meter-expiry-date"
              type="date"
              value={values.calibration_expiry_date}
              onChange={(e) => set("calibration_expiry_date", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
        </>
      )}
    </FormDialog>
  );
}
