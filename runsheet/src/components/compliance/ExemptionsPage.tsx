"use client";

/**
 * Settings → Company → Exemptions (UI revamp task 3.5): type in the Filters
 * popover, a DataTable with an expiry StatusBadge, and "Add exemption" as an
 * md FormDialog (design.md §5).
 */
import { Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  EntityLink,
  Field,
  FilterPopover,
  FormDialog,
  IconButton,
  INPUT_CLASS,
  Select,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { calendarDate } from "../../lib/format";
import {
  type CreateTaxExemptionPayload,
  createTaxExemption,
  getTaxExemptions,
  type TaxExemption,
} from "../../services/complianceApi";
import CustomerPicker from "../ops/CustomerPicker";
import { PageTitle } from "../ui/PageHeader";

// ─── Expiry status ───────────────────────────────────────────────────────────

type ExpiryStatus = "active" | "expiring_soon" | "expired";

export function getExpiryStatus(
  expiryDate: string,
  now: Date = new Date(),
): ExpiryStatus {
  const expiry = new Date(
    /^\d{4}-\d{2}-\d{2}$/.test(expiryDate)
      ? `${expiryDate}T23:59:59Z`
      : expiryDate,
  );
  const diffMs = expiry.getTime() - now.getTime();
  if (diffMs < 0) return "expired";
  const diffDays = Math.ceil(diffMs / 86_400_000);
  if (diffDays <= 30) return "expiring_soon";
  return "active";
}

const EXPIRY_BADGE = {
  active: { status: "ok", label: "Active" },
  expiring_soon: { status: "warning", label: "Expiring Soon" },
  expired: { status: "critical", label: "Expired" },
} as const;

function formatDate(dateStr: string | null): string {
  return dateStr ? calendarDate(dateStr) : "—";
}

// ─── Exemption types ─────────────────────────────────────────────────────────

const EXEMPTION_TYPES = [
  { value: "dyed_diesel", label: "Dyed Diesel (IRS 637M)" },
  { value: "farm_agricultural", label: "Farm / Agricultural" },
  { value: "road_use", label: "Road-Use Exemption" },
  { value: "government", label: "Government Entity" },
  { value: "nonprofit", label: "Nonprofit Organization" },
];

function getExemptionTypeLabel(type: string): string {
  return EXEMPTION_TYPES.find((t) => t.value === type)?.label ?? type;
}

const exemptionColumns: Column<TaxExemption>[] = [
  {
    key: "customer_id",
    header: "Customer",
    width: 200,
    // The exemption's subject is its customer, navigable to the Commerce
    // module (Req 11.3, 13.1).
    cell: (e) => (
      <EntityLink type="customer" id={e.customer_id} className="font-medium" />
    ),
  },
  {
    key: "exemption_type",
    header: "Type",
    truncate: true,
    cell: (e) => getExemptionTypeLabel(e.exemption_type),
  },
  {
    key: "certificate_number",
    header: "Certificate",
    width: 180,
    className: "font-mono text-xs",
    cell: (e) => e.certificate_number,
  },
  {
    key: "expiry_date",
    header: "Expires",
    width: 160,
    cell: (e) => formatDate(e.expiry_date),
  },
  {
    key: "status",
    header: "Status",
    width: 150,
    cell: (e) => {
      const b = EXPIRY_BADGE[getExpiryStatus(e.expiry_date)];
      return <StatusBadge status={b.status} label={b.label} />;
    },
  },
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function ExemptionsPage() {
  const [exemptions, setExemptions] = useState<TaxExemption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [typeFilter, setTypeFilter] = useState<string>("");
  const [reload, setReload] = useState(0);
  const [adding, setAdding] = useState(false);

  const fetchExemptions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: { exemption_type?: string; page: number; size: number } = {
        page,
        size: 20,
      };
      if (typeFilter) filters.exemption_type = typeFilter;
      const response = await getTaxExemptions(filters);
      setExemptions(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load exemptions",
      );
    } finally {
      setLoading(false);
    }
    // `reload` refetches after a create.
  }, [page, typeFilter, reload]);

  useEffect(() => {
    fetchExemptions();
  }, [fetchExemptions]);

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setAdding(true)}
      >
        Add Exemption
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Tax Exemption Certificates
          </PageTitle>
          <div className="ml-auto">{actions}</div>
        </div>
      )}
      <Toolbar
        label="Exemptions"
        filters={
          <FilterPopover
            count={typeFilter ? 1 : 0}
            label="Exemption filters"
            onClear={() => {
              setTypeFilter("");
              setPage(1);
            }}
          >
            <Field label="Exemption type" id="exemption-type-filter">
              <Select
                id="exemption-type-filter"
                value={typeFilter}
                onChange={(v) => {
                  setTypeFilter(v);
                  setPage(1);
                }}
                placeholder="All types"
                options={EXEMPTION_TYPES}
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
        <DataTable<TaxExemption>
          ariaLabel="Tax exemption certificates"
          columns={exemptionColumns}
          data={loading || error ? [] : exemptions}
          loading={loading}
          error={error ? { message: error, onRetry: fetchExemptions } : null}
          getRowId={(e) => e.exemption_id}
          pagination={
            totalPages > 1
              ? { page, totalPages, onPageChange: setPage }
              : undefined
          }
          emptyState={
            <span className="text-text-muted">
              No exemption certificates found.
            </span>
          }
        />
      </div>

      {adding && (
        <ExemptionDialog
          onClose={() => setAdding(false)}
          onSaved={() => setReload((n) => n + 1)}
        />
      )}
    </div>
  );
}

// ─── Add exemption dialog ────────────────────────────────────────────────────

type ExemptionValues = {
  customer_id: string;
  exemption_type: string;
  certificate_number: string;
  expiry_date: string;
};

export function validateExemption(v: ExemptionValues) {
  const errors: Record<string, string | undefined> = {};
  if (!v.customer_id) errors.customer_id = "Pick a customer.";
  if (!v.exemption_type) errors.exemption_type = "Pick a type.";
  if (!v.certificate_number.trim())
    errors.certificate_number = "Enter the certificate number.";
  if (!v.expiry_date) errors.expiry_date = "Enter the expiry date.";
  return errors;
}

function ExemptionDialog({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const submit = async (v: ExemptionValues) => {
    const data: CreateTaxExemptionPayload = {
      customer_id: v.customer_id,
      exemption_type: v.exemption_type,
      certificate_number: v.certificate_number.trim(),
      expiry_date: v.expiry_date,
    };
    await createTaxExemption(data);
  };
  return (
    <FormDialog<ExemptionValues, void>
      open
      size="md"
      title="Add exemption certificate"
      submitLabel="Add Exemption"
      successMessage="Exemption added"
      initialValues={{
        customer_id: "",
        exemption_type: "",
        certificate_number: "",
        expiry_date: "",
      }}
      validate={validateExemption}
      onSubmit={submit}
      onSaved={onSaved}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field label="Customer" required span={1} error={errors.customer_id}>
            <CustomerPicker
              id="customer-id"
              aria-label="Customer ID"
              value={values.customer_id || null}
              onChange={(value) => set("customer_id", value)}
            />
          </Field>
          <Field
            label="Exemption type"
            required
            span={1}
            error={errors.exemption_type}
          >
            <Select
              id="exemption-type"
              value={values.exemption_type}
              onChange={(v) => set("exemption_type", v)}
              placeholder="Select type…"
              options={EXEMPTION_TYPES}
            />
          </Field>
          <Field
            label="Certificate number"
            required
            span={1}
            error={errors.certificate_number}
          >
            <input
              id="certificate-number"
              type="text"
              value={values.certificate_number}
              onChange={(e) => set("certificate_number", e.target.value)}
              className={INPUT_CLASS}
              placeholder="e.g. 637M-12345"
            />
          </Field>
          <Field
            label="Expiry date"
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
        </>
      )}
    </FormDialog>
  );
}
