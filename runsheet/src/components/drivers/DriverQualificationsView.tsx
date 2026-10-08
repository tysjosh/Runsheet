"use client";

/**
 * Fleet → Drivers → Qualifications (UI revamp task 3.1).
 *
 * One toolbar (status chips with the DQF dashboard's counts, expiring count,
 * Export CSV), a DataTable whose "Alerts" column carries each driver's
 * qualification warnings from the DQF dashboard, detail in a Drawer, and
 * add/edit in a sectioned lg FormDialog (Identity, CDL, Medical,
 * Endorsements; design.md §5 "Driver"). The separate "DQF Dashboard" view is
 * relegated: its four summary cards are the chip counts and its per-driver
 * qualification badges are the Alerts column, so nothing is lost.
 */
import { Eye, Pencil, Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  Drawer,
  ExportCsvButton,
  Field,
  FilterChips,
  FormDialog,
  FormSection,
  INPUT_CLASS,
  Select,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { calendarDate, humanize, number } from "../../lib/format";
import {
  type CreateDriverPayload,
  createDriver,
  type DQFDashboard,
  type Driver,
  type DriverQualificationStatus,
  type DriverStatus,
  getDriver,
  getDrivers,
  getDriversDashboard,
  type UpdateDriverPayload,
  updateDriver,
} from "../../services/complianceApi";
import type { StatusKey } from "../../styles/tokens";

/** Driver status → badge style and label (icon + text). */
export const DRIVER_STATUS: Record<
  DriverStatus,
  { status: StatusKey; label: string }
> = {
  active: { status: "ok", label: "Active" },
  suspended: { status: "warning", label: "Suspended" },
  expired: { status: "critical", label: "Expired" },
};

const ALERT_STATUS: Record<
  DriverQualificationStatus["alert_level"],
  StatusKey | null
> = {
  ok: null,
  warning: "warning",
  urgent: "warning",
  critical: "critical",
  expired: "critical",
};

const QUAL_LABEL: Record<string, string> = {
  cdl: "CDL",
  medical_card: "Medical card",
  hazmat: "HAZMAT",
  tanker: "Tanker",
  drug_test: "Drug test",
  mvr: "MVR",
};
const qualLabel = (q: string) => QUAL_LABEL[q] ?? humanize(q);

const STATUS_IDS: DriverStatus[] = ["active", "suspended", "expired"];

function QualificationAlerts({
  quals,
}: {
  quals: DriverQualificationStatus[] | undefined;
}) {
  const alerts = (quals ?? []).filter((q) => ALERT_STATUS[q.alert_level]);
  if (alerts.length === 0)
    return <span className="text-xs text-text-muted">None</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {alerts.map((q) => (
        <StatusBadge
          key={q.qualification_type}
          status={ALERT_STATUS[q.alert_level] as StatusKey}
          label={
            q.alert_level === "expired" ||
            (q.days_until_expiry != null && q.days_until_expiry < 0)
              ? `${qualLabel(q.qualification_type)} expired`
              : q.days_until_expiry != null
                ? `${qualLabel(q.qualification_type)} · ${number(q.days_until_expiry)} d`
                : qualLabel(q.qualification_type)
          }
        />
      ))}
    </span>
  );
}

export default function DriverQualificationsView() {
  const [drivers, setDrivers] = useState<Driver[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [statusFilter, setStatusFilter] = useState<DriverStatus | "">("");
  const [dashboard, setDashboard] = useState<DQFDashboard | null>(null);
  const [selected, setSelected] = useState<Driver | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  // null = closed; { driver: null } = add; { driver } = edit.
  const [form, setForm] = useState<{ driver: Driver | null } | null>(null);
  const [reload, setReload] = useState(0);

  const fetchDrivers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: { status?: DriverStatus; page: number; size: number } = {
        page,
        size: 20,
      };
      if (statusFilter) filters.status = statusFilter;
      const response = await getDrivers(filters);
      setDrivers(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load drivers");
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter, reload]);

  useEffect(() => {
    fetchDrivers();
  }, [fetchDrivers]);

  // DQF dashboard: chip counts and per-driver qualification alerts. A failed
  // read leaves chips without counts and the Alerts column empty.
  useEffect(() => {
    let cancelled = false;
    getDriversDashboard()
      .then((r) => !cancelled && setDashboard(r.data))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [reload]);

  const qualsById = useMemo(() => {
    const m = new Map<string, DriverQualificationStatus[]>();
    for (const e of dashboard?.drivers ?? [])
      m.set(e.driver_id, e.qualifications ?? []);
    return m;
  }, [dashboard]);

  const openDetail = async (driverId: string) => {
    setDetailError(null);
    try {
      const response = await getDriver(driverId);
      setSelected(response.data);
    } catch (err) {
      setDetailError(
        err instanceof Error ? err.message : "Failed to load driver details",
      );
    }
  };

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setForm({ driver: null })}
      >
        Add driver
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  const counts: Record<string, number | undefined> = dashboard
    ? {
        all: dashboard.total_drivers,
        active: dashboard.active_drivers,
        suspended: dashboard.suspended_drivers,
        expired: dashboard.expired_drivers,
      }
    : {};

  const columns: Column<Driver>[] = [
    {
      key: "full_name",
      header: "Name",
      truncate: true,
      title: (d) => d.full_name,
      className: "font-medium text-text",
      cell: (d) => d.full_name,
    },
    {
      key: "cdl",
      header: "CDL",
      className: "text-slate-700 whitespace-nowrap",
      cell: (d) => (
        <span>
          <span className="font-mono text-xs">{d.cdl_number}</span>
          <span className="text-text-muted">
            {" "}
            · Class {d.cdl_class} · {d.cdl_state}
          </span>
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      width: 110,
      cell: (d) => {
        const cfg = DRIVER_STATUS[d.status] ?? DRIVER_STATUS.active;
        return <StatusBadge status={cfg.status} label={cfg.label} />;
      },
    },
    {
      key: "cdl_expiry_date",
      header: "CDL expiry",
      className: "text-slate-700 whitespace-nowrap",
      cell: (d) => calendarDate(d.cdl_expiry_date),
    },
    {
      key: "medical_card_expiry_date",
      header: "Medical expiry",
      className: "text-slate-700 whitespace-nowrap",
      cell: (d) => calendarDate(d.medical_card_expiry_date),
    },
    {
      key: "alerts",
      header: "Alerts",
      cell: (d) => <QualificationAlerts quals={qualsById.get(d.driver_id)} />,
    },
  ];

  return (
    <div className="flex h-full flex-col">
      <Toolbar
        label="Driver qualifications"
        filters={
          <FilterChips
            label="Driver status"
            options={[
              { id: "all", label: "All", count: counts.all },
              ...STATUS_IDS.map((id) => ({
                id,
                label: DRIVER_STATUS[id].label,
                count: counts[id],
                status: DRIVER_STATUS[id].status,
              })),
            ]}
            value={statusFilter || "all"}
            onChange={(v) => {
              setStatusFilter(v === "all" ? "" : (v as DriverStatus));
              setPage(1);
            }}
          />
        }
        end={
          <>
            {dashboard && dashboard.expiring_drivers > 0 && (
              <span className="whitespace-nowrap text-xs text-text-muted">
                {number(dashboard.expiring_drivers)} expiring within 60 days
              </span>
            )}
            {/* Qualification expiry dates per driver; admin only (OI-57). */}
            <ExportCsvButton
              type="driver_qualifications"
              params={{ status: statusFilter || undefined }}
              subject="driver qualifications"
              allowedRoles={["admin"]}
            />
            {!embedded && actions}
          </>
        }
      />
      {detailError && (
        <p
          role="alert"
          className="border-b border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800"
        >
          {detailError}
        </p>
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<Driver>
          ariaLabel="Drivers"
          columns={columns}
          data={loading || error ? [] : drivers}
          loading={loading}
          error={error ? { message: error, onRetry: fetchDrivers } : null}
          getRowId={(d) => d.driver_id}
          selectedId={selected?.driver_id}
          rowLabel={(d) => d.full_name}
          onRowClick={(d) => openDetail(d.driver_id)}
          rowMenu={(d) => [
            {
              id: "view",
              label: "View details",
              icon: <Eye className="h-3.5 w-3.5" />,
              onSelect: () => openDetail(d.driver_id),
            },
            {
              id: "edit",
              label: "Edit driver",
              icon: <Pencil className="h-3.5 w-3.5" />,
              onSelect: () => setForm({ driver: d }),
            },
          ]}
          pagination={
            totalPages > 1
              ? { page, totalPages, onPageChange: setPage }
              : undefined
          }
          emptyState={
            <div className="text-text-muted">
              <p className="text-sm font-medium">No drivers found</p>
            </div>
          }
        />
      </div>

      <Drawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.full_name ?? "Driver"}
        width={440}
        footer={
          selected ? (
            <Button
              icon={<Pencil className="h-3.5 w-3.5" />}
              onClick={() => {
                const d = selected;
                setSelected(null);
                setForm({ driver: d });
              }}
            >
              Edit driver
            </Button>
          ) : undefined
        }
      >
        {selected && (
          <dl className="space-y-3 text-sm">
            <div className="flex items-center gap-2">
              <StatusBadge
                status={
                  (DRIVER_STATUS[selected.status] ?? DRIVER_STATUS.active)
                    .status
                }
                label={
                  (DRIVER_STATUS[selected.status] ?? DRIVER_STATUS.active).label
                }
              />
              <span className="text-text-muted">
                CDL {selected.cdl_number} · Class {selected.cdl_class} ·{" "}
                {selected.cdl_state}
              </span>
            </div>
            {(
              [
                ["CDL expiry", selected.cdl_expiry_date],
                ["Medical card expiry", selected.medical_card_expiry_date],
                ["HAZMAT endorsement", selected.hazmat_endorsement_expiry_date],
                ["Tanker endorsement", selected.tanker_endorsement_expiry_date],
                ["Last drug test", selected.last_drug_test_date],
                ["Last MVR", selected.last_mvr_date],
              ] as const
            ).map(([label, value]) => (
              <div
                key={label}
                className="flex justify-between gap-4 border-b border-slate-100 pb-2"
              >
                <dt className="text-text-muted">{label}</dt>
                <dd className="font-medium text-text">{calendarDate(value)}</dd>
              </div>
            ))}
            <div>
              <dt className="mb-1 text-text-muted">Alerts</dt>
              <dd>
                <QualificationAlerts
                  quals={qualsById.get(selected.driver_id)}
                />
              </dd>
            </div>
          </dl>
        )}
      </Drawer>

      {form && (
        <DriverFormDialog
          driver={form.driver}
          onClose={() => setForm(null)}
          onSaved={() => {
            setForm(null);
            setReload((n) => n + 1);
          }}
        />
      )}
    </div>
  );
}

// ─── Driver FormDialog ───────────────────────────────────────────────────────

type DriverValues = {
  full_name: string;
  cdl_number: string;
  cdl_state: string;
  cdl_class: "A" | "B" | "C";
  cdl_expiry_date: string;
  medical_card_expiry_date: string;
  hazmat_endorsement_expiry_date: string;
  tanker_endorsement_expiry_date: string;
};

export function validateDriver(v: DriverValues) {
  const e: Record<string, string | undefined> = {};
  if (!v.full_name.trim()) e.full_name = "Enter the driver's name.";
  if (!v.cdl_number.trim()) e.cdl_number = "Enter the CDL number.";
  if (!/^[A-Za-z]{2}$/.test(v.cdl_state.trim()))
    e.cdl_state = "Use the two-letter state code.";
  if (!v.cdl_expiry_date) e.cdl_expiry_date = "Enter the CDL expiry date.";
  if (!v.medical_card_expiry_date)
    e.medical_card_expiry_date = "Enter the medical card expiry date.";
  return e;
}

export function DriverFormDialog({
  driver,
  onClose,
  onSaved,
}: {
  driver: Driver | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const initial: DriverValues = {
    full_name: driver?.full_name ?? "",
    cdl_number: driver?.cdl_number ?? "",
    cdl_state: driver?.cdl_state ?? "",
    cdl_class: driver?.cdl_class ?? "A",
    cdl_expiry_date: driver?.cdl_expiry_date ?? "",
    medical_card_expiry_date: driver?.medical_card_expiry_date ?? "",
    hazmat_endorsement_expiry_date:
      driver?.hazmat_endorsement_expiry_date ?? "",
    tanker_endorsement_expiry_date:
      driver?.tanker_endorsement_expiry_date ?? "",
  };
  const submit = async (v: DriverValues) => {
    const data: CreateDriverPayload = {
      full_name: v.full_name.trim(),
      cdl_number: v.cdl_number.trim(),
      cdl_state: v.cdl_state.trim().toUpperCase(),
      cdl_class: v.cdl_class,
      cdl_expiry_date: v.cdl_expiry_date,
      medical_card_expiry_date: v.medical_card_expiry_date,
      hazmat_endorsement_expiry_date: v.hazmat_endorsement_expiry_date || null,
      tanker_endorsement_expiry_date: v.tanker_endorsement_expiry_date || null,
    };
    if (driver)
      return updateDriver(driver.driver_id, data as UpdateDriverPayload);
    return createDriver(data);
  };
  const dateInput = (
    id: string,
    value: string,
    onChange: (v: string) => void,
  ) => (
    <input
      id={id}
      type="date"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={INPUT_CLASS}
    />
  );
  return (
    <FormDialog<DriverValues>
      open
      size="lg"
      title={driver ? "Edit driver" : "Add driver"}
      submitLabel={driver ? "Save changes" : "Add driver"}
      successMessage={driver ? "Driver saved" : "Driver added"}
      initialValues={initial}
      validate={validateDriver}
      onSubmit={submit}
      onSaved={onSaved}
      onClose={onClose}
      sections={[
        { id: "identity", title: "Identity" },
        { id: "cdl", title: "CDL" },
        { id: "medical", title: "Medical" },
        { id: "endorsements", title: "Endorsements" },
      ]}
    >
      {({ values, set, errors }) => (
        <>
          <FormSection id="identity" title="Identity">
            <Field label="Full name" required error={errors.full_name}>
              <input
                id="full-name"
                type="text"
                value={values.full_name}
                onChange={(e) => set("full_name", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
          </FormSection>
          <FormSection id="cdl" title="CDL">
            <Field
              label="CDL number"
              required
              error={errors.cdl_number}
              span={1}
            >
              <input
                id="cdl-number"
                type="text"
                value={values.cdl_number}
                onChange={(e) => set("cdl_number", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="CDL state" required error={errors.cdl_state} span={1}>
              <input
                id="cdl-state"
                type="text"
                maxLength={2}
                value={values.cdl_state}
                onChange={(e) => set("cdl_state", e.target.value.toUpperCase())}
                placeholder="TX"
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="CDL class" span={1} id="cdl-class">
              <Select
                id="cdl-class"
                value={values.cdl_class}
                onChange={(c) => set("cdl_class", c as "A" | "B" | "C")}
                options={[
                  { value: "A", label: "Class A" },
                  { value: "B", label: "Class B" },
                  { value: "C", label: "Class C" },
                ]}
              />
            </Field>
            <Field
              label="CDL expiry"
              required
              error={errors.cdl_expiry_date}
              span={1}
              id="cdl-expiry"
            >
              {dateInput("cdl-expiry", values.cdl_expiry_date, (v) =>
                set("cdl_expiry_date", v),
              )}
            </Field>
          </FormSection>
          <FormSection id="medical" title="Medical">
            <Field
              label="Medical card expiry"
              required
              error={errors.medical_card_expiry_date}
              span={1}
              id="medical-expiry"
            >
              {dateInput(
                "medical-expiry",
                values.medical_card_expiry_date,
                (v) => set("medical_card_expiry_date", v),
              )}
            </Field>
          </FormSection>
          <FormSection id="endorsements" title="Endorsements">
            <Field label="HAZMAT expiry" span={1} id="hazmat-expiry">
              {dateInput(
                "hazmat-expiry",
                values.hazmat_endorsement_expiry_date,
                (v) => set("hazmat_endorsement_expiry_date", v),
              )}
            </Field>
            <Field label="Tanker expiry" span={1} id="tanker-expiry">
              {dateInput(
                "tanker-expiry",
                values.tanker_endorsement_expiry_date,
                (v) => set("tanker_endorsement_expiry_date", v),
              )}
            </Field>
          </FormSection>
        </>
      )}
    </FormDialog>
  );
}
