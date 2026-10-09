"use client";

/**
 * Depot management (admin) page.
 *
 * Surfaces the tenant-configurable Depot entity introduced by the Fuel
 * Ops Hardening spec — Capability 2, Dynamic Dispatch & Replanning
 * (Requirements 2.2.1, 2.2.2, 2.2.7). Provides:
 *
 *  • Paginated list view of all depots for the tenant, wired to
 *    `GET /api/fuel/mvp/depots`.
 *  • Create / edit modal posting to `POST /api/fuel/mvp/depots` and
 *    `PATCH /api/fuel/mvp/depots/{depot_id}`. Inline delete with an
 *    undo-free confirmation prompt.
 *  • A prominent "depot_required" setup banner that renders whenever
 *    the tenant has zero depots OR no depot is flagged as default
 *    (Req 2.2.7 / design spec "tenants without a default depot SHALL
 *    be flagged in the admin UI with a 'depot_required' setup task").
 *  • A "Set as default" row action which PATCHes the target depot with
 *    `is_default: true`. The server may mirror that into the tenant
 *    config's `default_depot_id` — see
 *    {@link fuelApi.DepotUpdatePayload.is_default} for the follow-up.
 *
 * Styling mirrors the peer admin/ops pages under
 * `runsheet/src/components/ops/` (Tailwind utility classes, inline status
 * chips, toast system, `bg-black/30` modal overlays) so this page sits
 * visually alongside `CustomerTankPage` and `FuelDistributionPage`.
 *
 * Validates: Requirements 2.2.2, 2.2.7.
 */

import {
  Eye,
  MapPin,
  Pencil,
  Plus,
  RefreshCw,
  Star,
  Trash2,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  Field,
  FilterChips,
  FilterPopover,
  FormDialog,
  IconButton,
  INPUT_CLASS,
  InlineBanner,
  Modal,
  NumberField,
  ProductCap,
  ProductChip,
  Select,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { number, productName } from "../../lib/format";
import {
  createDepot,
  type Depot,
  type DepotCreatePayload,
  type DepotListFilters,
  type DepotStatus,
  type DepotUpdatePayload,
  deleteDepot,
  listDepots,
  updateDepot,
} from "../../services/fuelApi";
import { PRODUCT_CODES } from "../../styles/tokens";
import { PageTitle } from "../ui/PageHeader";
import { notify } from "../ui/toast/notify";

// ─── Constants ───────────────────────────────────────────────────────────────

const PAGE_SIZE = 20;

const STATUSES: { value: DepotStatus; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "inactive", label: "Inactive" },
];

const STATUS_CHIPS: { id: "" | DepotStatus; label: string }[] = [
  { id: "", label: "All" },
  { id: "active", label: "Active" },
  { id: "inactive", label: "Inactive" },
];

const DEPOT_STATUS = {
  active: { status: "ok", label: "Active" },
  inactive: { status: "draft", label: "Inactive" },
} as const;

// A conservative, commonly-used subset of IANA tz identifiers surfaced as
// a datalist. Users can still type any valid IANA name — the backend
// validates via `zoneinfo.ZoneInfo(...)` at write time.
const COMMON_TIMEZONES: string[] = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Phoenix",
  "America/Anchorage",
  "Pacific/Honolulu",
  "Europe/London",
  "Europe/Paris",
];

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * A depot is the tenant default when its record's `is_default` flag is set.
 * The backend :class:`fuel.depot_models.Depot` model round-trips `is_default`
 * on every read (list and single-depot reads), and the `Depot` type now
 * declares the field, so this reads it directly rather than inferring the
 * default from a loosely-typed shape (cross-module-entity-linkage Req 10.3).
 */
export function isDefaultDepot(depot: Depot): boolean {
  return depot.is_default === true;
}

function formatCoordinates(depot: Depot): string {
  return `${number(depot.location_lat, { decimals: 5 })}, ${number(depot.location_lon, { decimals: 5 })}`;
}

export interface DepotFilters {
  status?: DepotStatus;
  fuel_type?: string;
}

// ─── Form Validation ─────────────────────────────────────────────────────────

export interface DepotFormValues {
  depot_id: string;
  name: string;
  location_lat: number;
  location_lon: number;
  address: string;
  timezone: string;
  fuel_types_supported: string[];
  status: DepotStatus;
}

export interface DepotFormErrors {
  name?: string;
  location_lat?: string;
  location_lon?: string;
  address?: string;
  timezone?: string;
  fuel_types_supported?: string;
}

/**
 * Pure validation for the create/edit form. Mirrors backend
 * :class:`Depot` field-level constraints so the UI catches obvious
 * mistakes before the round-trip.
 *
 * Validates: Requirement 2.2.1 (coordinate ranges, required name / address /
 * timezone, ≥1 fuel_type_supported per the task's validation spec).
 */
export function validateDepotForm(values: DepotFormValues): DepotFormErrors {
  const errors: DepotFormErrors = {};

  if (!values.name || !values.name.trim()) {
    errors.name = "Depot name is required.";
  }
  if (
    values.location_lat == null ||
    Number.isNaN(values.location_lat) ||
    values.location_lat < -90 ||
    values.location_lat > 90
  ) {
    errors.location_lat = "Latitude must be between -90 and 90.";
  }
  if (
    values.location_lon == null ||
    Number.isNaN(values.location_lon) ||
    values.location_lon < -180 ||
    values.location_lon > 180
  ) {
    errors.location_lon = "Longitude must be between -180 and 180.";
  }
  if (!values.address || !values.address.trim()) {
    errors.address = "Address is required.";
  }
  if (!values.timezone || !values.timezone.trim()) {
    errors.timezone =
      "Timezone is required (IANA format, e.g. America/Chicago).";
  }
  if (
    !values.fuel_types_supported ||
    values.fuel_types_supported.length === 0
  ) {
    errors.fuel_types_supported = "Select at least one supported fuel product.";
  }

  return errors;
}

// ─── Create / Edit dialog ────────────────────────────────────────────────────

type DialogValues = Omit<DepotFormValues, "location_lat" | "location_lon"> & {
  location_lat: number | null;
  location_lon: number | null;
};

function DepotDialog({
  depot,
  onClose,
  onSaved,
}: {
  depot: Depot | null;
  onClose: () => void;
  onSaved: (depot: Depot | null, mode: "create" | "edit") => void;
}) {
  const mode = depot ? "edit" : "create";
  const products = [
    ...PRODUCT_CODES,
    ...(depot?.fuel_types_supported ?? []).filter(
      (c) => !(PRODUCT_CODES as readonly string[]).includes(c),
    ),
  ];

  const submit = async (v: DialogValues): Promise<Depot | null> => {
    const lat = v.location_lat ?? Number.NaN;
    const lon = v.location_lon ?? Number.NaN;
    if (!depot) {
      const payload: DepotCreatePayload = {
        name: v.name.trim(),
        location_lat: lat,
        location_lon: lon,
        address: v.address.trim(),
        timezone: v.timezone.trim(),
        fuel_types_supported: v.fuel_types_supported,
        status: v.status,
      };
      if (v.depot_id.trim()) payload.depot_id = v.depot_id.trim();
      return createDepot(payload);
    }
    const patch: DepotUpdatePayload = {};
    if (v.name.trim() !== depot.name) patch.name = v.name.trim();
    if (lat !== depot.location_lat) patch.location_lat = lat;
    if (lon !== depot.location_lon) patch.location_lon = lon;
    if (v.address.trim() !== depot.address) patch.address = v.address.trim();
    if (v.timezone.trim() !== depot.timezone)
      patch.timezone = v.timezone.trim();
    if (
      !arraysShallowEqual(
        v.fuel_types_supported,
        depot.fuel_types_supported ?? [],
      )
    )
      patch.fuel_types_supported = v.fuel_types_supported;
    if (v.status !== depot.status) patch.status = v.status;
    // Nothing changed: close without touching the server.
    if (Object.keys(patch).length === 0) return null;
    return updateDepot(depot.depot_id, patch);
  };

  return (
    <FormDialog<DialogValues, Depot | null>
      open
      size="md"
      title={mode === "create" ? "Add depot" : "Edit depot"}
      help="Loading yards trucks start and end at. The route solver uses the tenant default when a truck has no depot."
      submitLabel={mode === "create" ? "Create depot" : "Save changes"}
      successMessage={null}
      initialValues={{
        depot_id: depot?.depot_id ?? "",
        name: depot?.name ?? "",
        location_lat: depot?.location_lat ?? null,
        location_lon: depot?.location_lon ?? null,
        address: depot?.address ?? "",
        timezone: depot?.timezone ?? "America/Chicago",
        fuel_types_supported: depot?.fuel_types_supported
          ? [...depot.fuel_types_supported]
          : [],
        status: depot?.status ?? "active",
      }}
      validate={(v) =>
        validateDepotForm({
          ...v,
          location_lat: v.location_lat ?? Number.NaN,
          location_lon: v.location_lon ?? Number.NaN,
        }) as Record<string, string | undefined>
      }
      onSubmit={submit}
      onSaved={(d) => onSaved(d, mode)}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field label="Name" required span={1} error={errors.name}>
            <input
              id="dp-name"
              type="text"
              className={INPUT_CLASS}
              value={values.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="e.g. Chicago Main Yard"
            />
          </Field>
          <Field
            label="Depot ID"
            span={1}
            help={mode === "create" ? "Generated if blank" : "Can't be changed"}
          >
            <input
              id="dp-depot-id"
              type="text"
              className={INPUT_CLASS}
              value={values.depot_id}
              onChange={(e) => set("depot_id", e.target.value)}
              disabled={mode === "edit"}
            />
          </Field>
          <Field label="Address" required error={errors.address}>
            <input
              id="dp-address"
              type="text"
              className={INPUT_CLASS}
              value={values.address}
              onChange={(e) => set("address", e.target.value)}
              placeholder="e.g. 1000 N Halsted St, Chicago, IL 60642"
            />
          </Field>
          <Field label="Latitude" required span={1} error={errors.location_lat}>
            <NumberField
              id="dp-lat"
              value={values.location_lat}
              onChange={(n) => set("location_lat", n)}
              decimals={5}
              min={-90}
              max={90}
              placeholder="41.87810"
            />
          </Field>
          <Field
            label="Longitude"
            required
            span={1}
            error={errors.location_lon}
          >
            <NumberField
              id="dp-lon"
              value={values.location_lon}
              onChange={(n) => set("location_lon", n)}
              decimals={5}
              min={-180}
              max={180}
              placeholder="-87.62980"
            />
          </Field>
          <Field
            label="Timezone"
            required
            span={1}
            help="IANA name"
            error={errors.timezone}
          >
            <input
              id="dp-timezone"
              type="text"
              list="dp-timezone-options"
              className={INPUT_CLASS}
              value={values.timezone}
              onChange={(e) => set("timezone", e.target.value)}
              placeholder="America/Chicago"
            />
          </Field>
          <datalist id="dp-timezone-options">
            {COMMON_TIMEZONES.map((tz) => (
              <option key={tz} value={tz} />
            ))}
          </datalist>
          <Field label="Status" span={1}>
            <Select
              id="dp-status"
              value={values.status}
              onChange={(v) => set("status", v as DepotStatus)}
              options={STATUSES}
            />
          </Field>
          <fieldset className="col-span-2">
            <legend className="mb-1 text-xs font-medium text-slate-700">
              Products this depot can load{" "}
              <span className="text-red-700">*</span>
            </legend>
            <div className="grid grid-cols-2 gap-1.5">
              {products.map((code) => {
                const checked = values.fuel_types_supported.includes(code);
                return (
                  <label
                    key={code}
                    className="flex items-center gap-2 rounded-md px-1.5 py-1 text-sm hover:bg-slate-50"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() =>
                        set(
                          "fuel_types_supported",
                          checked
                            ? values.fuel_types_supported.filter(
                                (c) => c !== code,
                              )
                            : [...values.fuel_types_supported, code],
                        )
                      }
                    />
                    <ProductCap code={code} decorative />
                    <span>{productName(code)}</span>
                  </label>
                );
              })}
            </div>
            {errors.fuel_types_supported && (
              <p role="alert" className="mt-1 text-xs text-red-800">
                {errors.fuel_types_supported}
              </p>
            )}
          </fieldset>
        </>
      )}
    </FormDialog>
  );
}

// ─── Main Page ───────────────────────────────────────────────────────────────

export interface DepotsPageProps {
  /** Initial filter state; useful when linking in from another page. */
  initialFilters?: DepotFilters;
}

export default function DepotsPage({ initialFilters }: DepotsPageProps = {}) {
  const router = useRouter();
  const [filters, setFilters] = useState<DepotFilters>(initialFilters ?? {});
  const [page, setPage] = useState(1);
  const [depots, setDepots] = useState<Depot[]>([]);
  const [totalWindow, setTotalWindow] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // null = closed; "new" = create; otherwise the depot being edited.
  const [editing, setEditing] = useState<null | "new" | Depot>(null);
  const [deleteTarget, setDeleteTarget] = useState<Depot | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  const loadDepots = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError("");
      try {
        const query: DepotListFilters = { ...filters, page, size: PAGE_SIZE };
        const response = await listDepots(query);
        if (signal?.aborted) return;
        setDepots(response.items);
        setTotalWindow(response.total);
        setHasNext(response.has_next);
      } catch (err) {
        if (signal?.aborted) return;
        setError(err instanceof Error ? err.message : "Failed to load depots.");
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [filters, page],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadDepots(controller.signal);
    return () => controller.abort();
  }, [loadDepots]);

  const setFiltersAndReset = (next: DepotFilters) => {
    setFilters(next);
    setPage(1);
  };

  const handleSetDefault = useCallback(
    async (depot: Depot) => {
      try {
        // If the backend hasn't wired is_default through to
        // tenant_settings.default_depot_id, this PATCH 422s and the toast
        // says so; the list is left unchanged.
        await updateDepot(depot.depot_id, { is_default: true });
        notify({
          type: "success",
          message: `Set ${depot.name} as tenant default.`,
        });
        loadDepots();
      } catch (err) {
        notify({
          type: "error",
          message:
            err instanceof Error
              ? `Set-as-default failed: ${err.message}`
              : "Set-as-default failed.",
        });
      }
    },
    [loadDepots],
  );

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await deleteDepot(deleteTarget.depot_id);
      notify({
        type: "success",
        message: `Deleted depot ${deleteTarget.depot_id}.`,
      });
      setDeleteTarget(null);
      loadDepots();
    } catch (err) {
      setDeleteError(
        err instanceof Error ? err.message : "Failed to delete depot.",
      );
    } finally {
      setDeleting(false);
    }
  };

  // "depot_required" banner: zero depots OR none flagged default, only on
  // page 1 with no filters (a narrow filter's "no results" isn't setup).
  const bannerReason: "empty" | "no_default" | null = useMemo(() => {
    const filtersActive = !!filters.status || !!filters.fuel_type || page !== 1;
    if (filtersActive) return null;
    if (!loading && depots.length === 0 && totalWindow === 0) return "empty";
    if (depots.length > 0 && !depots.some(isDefaultDepot)) return "no_default";
    return null;
  }, [filters, page, loading, depots, totalWindow]);

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setEditing("new")}
      >
        Add Depot
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  const openDetail = (d: Depot) =>
    router.push(`/dashboard/settings/depots/${encodeURIComponent(d.depot_id)}`);

  const columns: Column<Depot>[] = [
    {
      key: "depot",
      header: "Depot",
      truncate: true,
      title: (d) => d.depot_id,
      cell: (d) => (
        <span className="inline-flex min-w-0 items-center gap-2">
          <span className="truncate font-medium text-text">{d.name}</span>
          {isDefaultDepot(d) && (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-amber-300 bg-amber-100 px-1.5 text-[11px] font-semibold text-amber-800">
              <Star aria-hidden="true" className="h-3 w-3" />
              Default
            </span>
          )}
        </span>
      ),
    },
    {
      key: "address",
      header: "Location",
      truncate: true,
      title: (d) => `${d.address} (${formatCoordinates(d)})`,
      cell: (d) => (
        <span className="inline-flex min-w-0 items-center gap-1 text-slate-700">
          <MapPin aria-hidden="true" className="h-3 w-3 shrink-0" />
          <span className="truncate">{d.address}</span>
        </span>
      ),
    },
    {
      key: "timezone",
      header: "Timezone",
      width: 170,
      className: "text-slate-700",
      cell: (d) => d.timezone,
    },
    {
      key: "products",
      header: "Products",
      width: 220,
      cell: (d) => {
        const codes = d.fuel_types_supported ?? [];
        if (codes.length === 0) return "—";
        if (codes.length === 1) return <ProductChip code={codes[0]} />;
        return (
          <span
            className="flex items-center gap-1"
            title={codes.map((c) => productName(c)).join(", ")}
          >
            {codes.map((c) => (
              <ProductCap key={c} code={c} />
            ))}
          </span>
        );
      },
    },
    {
      key: "status",
      header: "Status",
      width: 120,
      cell: (d) => {
        const s = DEPOT_STATUS[d.status] ?? DEPOT_STATUS.inactive;
        return <StatusBadge status={s.status} label={s.label} />;
      },
    },
  ];

  const popoverCount = filters.fuel_type ? 1 : 0;

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Depots
          </PageTitle>
          <div className="ml-auto">{actions}</div>
        </div>
      )}
      <Toolbar
        label="Depots"
        filters={
          <>
            <FilterChips
              label="Depot status"
              options={STATUS_CHIPS.map((c) => ({
                id: c.id || "all",
                label: c.label,
                status: c.id ? DEPOT_STATUS[c.id].status : undefined,
              }))}
              value={filters.status ?? "all"}
              onChange={(v) =>
                setFiltersAndReset({
                  ...filters,
                  status: v === "all" ? undefined : (v as DepotStatus),
                })
              }
            />
            <FilterPopover
              count={popoverCount}
              label="Depot filters"
              onClear={() =>
                setFiltersAndReset({ ...filters, fuel_type: undefined })
              }
            >
              <Field label="Supports product" id="dp-filter-fuel-type">
                <Select
                  id="dp-filter-fuel-type"
                  value={filters.fuel_type ?? ""}
                  onChange={(v) =>
                    setFiltersAndReset({
                      ...filters,
                      fuel_type: v || undefined,
                    })
                  }
                  placeholder="Any product"
                  options={PRODUCT_CODES.map((c) => ({
                    value: c,
                    label: productName(c),
                  }))}
                />
              </Field>
            </FilterPopover>
          </>
        }
        end={
          <IconButton
            label="Refresh depots"
            size="sm"
            onClick={() => loadDepots()}
            icon={
              <RefreshCw
                className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
              />
            }
          />
        }
      />
      {bannerReason && (
        <div
          className="px-4 pt-3"
          data-testid="depot-required-banner"
          data-reason={bannerReason}
        >
          <InlineBanner
            tone="warning"
            action={
              <Button size="sm" onClick={() => setEditing("new")}>
                {bannerReason === "empty"
                  ? "Create first depot"
                  : "Pick a default"}
              </Button>
            }
          >
            {bannerReason === "empty"
              ? "Depot setup required: the route solver can't plan until at least one depot exists."
              : "Default depot required: trucks with no depot fall back to the default, and none is set."}
          </InlineBanner>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<Depot>
          ariaLabel="Depot list"
          columns={columns}
          data={loading || error ? [] : depots}
          loading={loading && depots.length === 0}
          error={error ? { message: error, onRetry: () => loadDepots() } : null}
          getRowId={(d) => d.depot_id}
          rowLabel={(d) => `Depot ${d.name}`}
          onRowClick={openDetail}
          rowMenu={(d) => {
            const isDefault = isDefaultDepot(d);
            return [
              {
                id: "view",
                label: "View depot",
                icon: <Eye className="h-3.5 w-3.5" />,
                onSelect: () => openDetail(d),
              },
              {
                id: "edit",
                label: "Edit",
                icon: <Pencil className="h-3.5 w-3.5" />,
                onSelect: () => setEditing(d),
              },
              {
                id: "default",
                label: isDefault ? "Tenant default" : "Set as default",
                icon: <Star className="h-3.5 w-3.5" />,
                disabled: isDefault,
                onSelect: () => void handleSetDefault(d),
              },
              {
                id: "delete",
                label: "Delete",
                danger: true,
                icon: <Trash2 className="h-3.5 w-3.5" />,
                onSelect: () => {
                  setDeleteError("");
                  setDeleteTarget(d);
                },
              },
            ];
          }}
          pagination={
            hasNext || page > 1
              ? {
                  page,
                  totalPages: hasNext ? page + 1 : page,
                  onPageChange: setPage,
                }
              : undefined
          }
          emptyState={
            <div className="text-text-muted">
              <p className="text-sm font-medium">No depots found</p>
              <p className="mt-1 text-xs">
                Try adjusting your filters or add a new depot.
              </p>
            </div>
          }
        />
      </div>

      {editing && (
        <DepotDialog
          depot={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(d, mode) => {
            if (d)
              notify({
                type: "success",
                message:
                  mode === "create"
                    ? `Created depot ${d.depot_id}.`
                    : `Updated depot ${d.depot_id}.`,
              });
            loadDepots();
          }}
        />
      )}

      <Modal
        isOpen={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete depot?"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteTarget(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={deleting}
              icon={<Trash2 className="h-3.5 w-3.5" />}
              onClick={confirmDelete}
            >
              Delete
            </Button>
          </>
        }
      >
        {deleteTarget && (
          <div className="space-y-3 px-6 py-4 text-sm">
            <p className="text-slate-700">
              This removes{" "}
              <span className="font-semibold text-text">
                {deleteTarget.name}
              </span>{" "}
              ({deleteTarget.depot_id}). Trucks assigned to it fall back to the
              tenant default on the next plan.
            </p>
            {deleteError && (
              <p role="alert" className="text-red-800">
                {deleteError}
              </p>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}

// ─── Internal Utilities ──────────────────────────────────────────────────────

function arraysShallowEqual(a: string[], b: string[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  const sortedA = [...a].sort();
  const sortedB = [...b].sort();
  for (let i = 0; i < sortedA.length; i++) {
    if (sortedA[i] !== sortedB[i]) return false;
  }
  return true;
}
