"use client";

/**
 * Compliance → BOLs (UI revamp task 3.5): status chips, product and driver in
 * the Filters popover, a DataTable with products by name and gallons through
 * `lib/format`, and "Upload BOL" as an md FormDialog (design.md §5).
 */
import { RefreshCw, Upload } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  EntityLink,
  Field,
  FilterChips,
  FilterPopover,
  FormDialog,
  IconButton,
  ProductChip,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { dateTime, gallons } from "../../lib/format";
import {
  getTerminalBOLs,
  type TerminalBOL,
  type TerminalBOLStatus,
  uploadTerminalBOL,
} from "../../services/complianceApi";
import type { StatusKey } from "../../styles/tokens";
import DriverPicker from "../ops/DriverPicker";
import ProductPicker from "../ops/ProductPicker";
import { PageTitle } from "../ui/PageHeader";

const BOL_STATUS: Record<
  TerminalBOLStatus,
  { status: StatusKey; label: string }
> = {
  ingested: { status: "draft", label: "Ingested" },
  pending_confirmation: { status: "warning", label: "Pending confirmation" },
  linked: { status: "ok", label: "Linked" },
};

const STATUS_CHIPS: { id: "" | TerminalBOLStatus; label: string }[] = [
  { id: "", label: "All" },
  { id: "pending_confirmation", label: "Pending" },
  { id: "ingested", label: "Ingested" },
  { id: "linked", label: "Linked" },
];

const bolColumns: Column<TerminalBOL>[] = [
  {
    key: "load_number",
    header: "Load",
    width: 140,
    className: "font-medium text-text",
    cell: (bol) => bol.load_number,
  },
  {
    key: "status",
    header: "Status",
    width: 190,
    cell: (bol) => {
      const s = BOL_STATUS[bol.status] ?? {
        status: "draft" as StatusKey,
        label: String(bol.status).replace(/_/g, " "),
      };
      return <StatusBadge status={s.status} label={s.label} />;
    },
  },
  {
    key: "product_code",
    header: "Product",
    width: 200,
    cell: (bol) => <ProductChip code={bol.product_code} />,
  },
  {
    key: "gross_gallons",
    header: "Gross",
    align: "right",
    width: 110,
    className: "tabular-nums",
    cell: (bol) => gallons(bol.gross_gallons, { decimals: 1 }),
  },
  {
    key: "net_gallons",
    header: "Net",
    align: "right",
    width: 110,
    className: "tabular-nums",
    cell: (bol) => gallons(bol.net_gallons, { decimals: 1 }),
  },
  {
    key: "supplier_name",
    header: "Supplier",
    truncate: true,
    title: (bol) => bol.supplier_name,
    cell: (bol) => bol.supplier_name,
  },
  {
    key: "terminal_name",
    header: "Terminal",
    truncate: true,
    title: (bol) => bol.terminal_name,
    cell: (bol) => bol.terminal_name,
  },
  {
    key: "driver_id",
    header: "Driver",
    width: 150,
    // The terminal BOL's subject is its driver, navigable to the Drivers
    // module (Req 11.3, 13.1).
    cell: (bol) => <EntityLink type="driver" id={bol.driver_id} />,
  },
  {
    key: "timestamp",
    header: "Received",
    width: 150,
    cell: (bol) => dateTime(bol.timestamp),
  },
];

export default function TerminalBOLsPage() {
  const [bols, setBols] = useState<TerminalBOL[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [reload, setReload] = useState(0);
  const [statusFilter, setStatusFilter] = useState<"" | TerminalBOLStatus>("");
  const [productCodeFilter, setProductCodeFilter] = useState<string>("");
  const [driverIdFilter, setDriverIdFilter] = useState<string>("");
  const [uploading, setUploading] = useState(false);

  const fetchBOLs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: {
        status?: TerminalBOLStatus;
        product_code?: string;
        driver_id?: string;
        page: number;
        size: number;
      } = { page, size: 20 };
      if (statusFilter) filters.status = statusFilter;
      if (productCodeFilter) filters.product_code = productCodeFilter;
      if (driverIdFilter) filters.driver_id = driverIdFilter;
      const response = await getTerminalBOLs(filters);
      setBols(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load terminal BOLs",
      );
    } finally {
      setLoading(false);
    }
    // `reload` refetches after an upload.
  }, [page, statusFilter, productCodeFilter, driverIdFilter, reload]);

  useEffect(() => {
    fetchBOLs();
  }, [fetchBOLs]);

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Upload className="h-3.5 w-3.5" />}
        onClick={() => setUploading(true)}
      >
        Upload BOL
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  const popoverCount = (productCodeFilter ? 1 : 0) + (driverIdFilter ? 1 : 0);

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Terminal BOLs
          </PageTitle>
          <div className="ml-auto">{actions}</div>
        </div>
      )}
      <Toolbar
        label="Terminal BOLs"
        filters={
          <>
            <FilterChips
              label="BOL status"
              options={STATUS_CHIPS.map((c) => ({
                id: c.id || "all",
                label: c.label,
                status: c.id ? BOL_STATUS[c.id].status : undefined,
              }))}
              value={statusFilter || "all"}
              onChange={(v) => {
                setStatusFilter(v === "all" ? "" : (v as TerminalBOLStatus));
                setPage(1);
              }}
            />
            <FilterPopover
              count={popoverCount}
              label="BOL filters"
              onClear={() => {
                setProductCodeFilter("");
                setDriverIdFilter("");
                setPage(1);
              }}
            >
              <div className="grid w-80 gap-3">
                <Field label="Product" id="product-code-filter">
                  <ProductPicker
                    id="product-code-filter"
                    aria-label="Product Code"
                    value={productCodeFilter || null}
                    onChange={(value) => {
                      setProductCodeFilter(value);
                      setPage(1);
                    }}
                    placeholder="All products"
                    allowClear
                  />
                </Field>
                <Field label="Driver" id="driver-id-filter">
                  <DriverPicker
                    id="driver-id-filter"
                    aria-label="Driver ID"
                    value={driverIdFilter || null}
                    onChange={(value) => {
                      setDriverIdFilter(value);
                      setPage(1);
                    }}
                    placeholder="All drivers"
                    allowClear
                  />
                </Field>
              </div>
            </FilterPopover>
          </>
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
        <DataTable<TerminalBOL>
          ariaLabel="Terminal BOLs"
          columns={bolColumns}
          data={loading || error ? [] : bols}
          loading={loading}
          error={error ? { message: error, onRetry: fetchBOLs } : null}
          getRowId={(bol) => bol.bol_id}
          pagination={
            totalPages > 1
              ? { page, totalPages, onPageChange: setPage }
              : undefined
          }
          emptyState={
            <span className="text-text-muted">No terminal BOLs found.</span>
          }
        />
      </div>

      {uploading && (
        <FormDialog<{ file: File | null }, void>
          open
          size="md"
          title="Upload terminal BOL"
          help="A scanned BOL (PDF or image). Its data is read by OCR and saved as pending confirmation."
          submitLabel="Upload BOL"
          successMessage="BOL uploaded"
          initialValues={{ file: null }}
          validate={(v) => ({
            file: v.file ? undefined : "Choose a BOL document.",
          })}
          onSubmit={async (v) => {
            if (v.file) await uploadTerminalBOL(v.file);
          }}
          onSaved={() => setReload((n) => n + 1)}
          onClose={() => setUploading(false)}
        >
          {({ set, errors }) => (
            <Field
              label="BOL document"
              required
              help="PDF, PNG, JPG or TIFF"
              error={errors.file}
            >
              <input
                id="bol-file"
                type="file"
                accept=".pdf,.png,.jpg,.jpeg,.tiff,.tif"
                onChange={(e) => set("file", e.target.files?.[0] ?? null)}
                className="block w-full text-sm"
              />
            </Field>
          )}
        </FormDialog>
      )}
    </div>
  );
}
