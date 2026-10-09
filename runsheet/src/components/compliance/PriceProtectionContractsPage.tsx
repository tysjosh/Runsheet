"use client";

/**
 * Billing → Contracts (price protection; UI revamp task 3.5): status chips,
 * contract type and the market price for the variance in the Filters
 * popover, a DataTable with products by name and prices in dollars, and
 * create/edit as an lg FormDialog (design.md §5). Prices are stored in cents
 * per gallon; the dialog edits dollars with 2 decimals.
 */
import { Pencil, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { SearchableSelectOption } from "@/components/ui";
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
  INPUT_CLASS,
  NumberField,
  ProductChip,
  SearchableSelect,
  Select,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { calendarDate, gallons, money } from "../../lib/format";
import { getAccounts } from "../../services/commerceApi";
import {
  type ContractStatus,
  type ContractType,
  type CreatePriceProtectionContractPayload,
  createPriceProtectionContract,
  getPriceProtectionContracts,
  type PriceProtectionContract,
  type UpdatePriceProtectionContractPayload,
  updatePriceProtectionContract,
} from "../../services/complianceApi";
import type { StatusKey } from "../../styles/tokens";
import CustomerPicker from "../ops/CustomerPicker";
import ProductPicker from "../ops/ProductPicker";
import { PageTitle } from "../ui/PageHeader";

const CONTRACT_STATUS: Record<
  ContractStatus,
  { status: StatusKey; label: string }
> = {
  active: { status: "ok", label: "Active" },
  exhausted: { status: "warning", label: "Exhausted" },
  expired: { status: "cancelled", label: "Expired" },
};

const CONTRACT_TYPES: { value: ContractType; label: string }[] = [
  { value: "fixed_price", label: "Fixed price" },
  { value: "cap_price", label: "Cap price" },
  { value: "collar", label: "Collar" },
];

function contractTypeLabel(type: ContractType): string {
  return CONTRACT_TYPES.find((t) => t.value === type)?.label ?? type;
}

function formatDate(dateStr: string | null): string {
  return dateStr ? calendarDate(dateStr) : "—";
}

/** Cents per gallon → "$3.50". */
function centsPrice(cents: number | null | undefined): string {
  return cents == null ? "—" : money(cents / 100);
}

/** The contract's price terms in one cell: "$3.50", "≤ $4.00", "$3.00–$4.00". */
function priceTerms(c: PriceProtectionContract): string {
  if (c.contract_type === "fixed_price") return centsPrice(c.fixed_price_cents);
  if (c.contract_type === "cap_price")
    return `≤ ${centsPrice(c.price_cap_cents)}`;
  return `${centsPrice(c.price_floor_cents)}–${centsPrice(c.price_cap_cents)}`;
}

// ─── Settlement Variance Computation ─────────────────────────────────────────

interface SettlementVariance {
  marketPriceCents: number;
  effectivePriceCents: number;
  gallonsDelivered: number;
  varianceCents: number;
}

/**
 * Computes settlement variance: (market_price - effective_price) × gallons
 * Positive = gain for distributor (market > contract), Negative = loss
 */
function computeSettlementVariance(
  contract: PriceProtectionContract,
  marketPriceCents: number,
): SettlementVariance {
  let effectivePriceCents: number;

  switch (contract.contract_type) {
    case "fixed_price":
      effectivePriceCents = contract.fixed_price_cents ?? 0;
      break;
    case "cap_price":
      effectivePriceCents = Math.min(
        marketPriceCents,
        contract.price_cap_cents ?? marketPriceCents,
      );
      break;
    case "collar":
      effectivePriceCents = Math.max(
        contract.price_floor_cents ?? 0,
        Math.min(
          marketPriceCents,
          contract.price_cap_cents ?? marketPriceCents,
        ),
      );
      break;
    default:
      effectivePriceCents = marketPriceCents;
  }

  const gallonsDelivered =
    contract.contracted_gallons - contract.remaining_gallons;
  const varianceCents =
    (marketPriceCents - effectivePriceCents) * gallonsDelivered;

  return {
    marketPriceCents,
    effectivePriceCents,
    gallonsDelivered,
    varianceCents,
  };
}

// ─── Settlement Variance Cell ────────────────────────────────────────────────

function renderVarianceCell(
  contract: PriceProtectionContract,
  marketPriceCents: number,
) {
  const variance = computeSettlementVariance(contract, marketPriceCents);
  if (variance.gallonsDelivered === 0) {
    return <span className="text-text-muted">No deliveries</span>;
  }

  const varianceDollars = variance.varianceCents / 100;
  const isPositive = varianceDollars >= 0;

  return (
    <div className="text-sm">
      <span
        className={`font-medium tabular-nums ${isPositive ? "text-emerald-800" : "text-red-800"}`}
      >
        {isPositive ? "+" : "−"}
        {money(Math.abs(varianceDollars))} {isPositive ? "gain" : "loss"}
      </span>
      <span className="ml-1 text-xs text-text-muted">
        ({gallons(variance.gallonsDelivered, { decimals: 0 })})
      </span>
    </div>
  );
}

// ─── Main Component ──────────────────────────────────────────────────────────

const STATUS_CHIPS: { id: "" | ContractStatus; label: string }[] = [
  { id: "", label: "All" },
  { id: "active", label: "Active" },
  { id: "exhausted", label: "Exhausted" },
  { id: "expired", label: "Expired" },
];

export default function PriceProtectionContractsPage() {
  const [contracts, setContracts] = useState<PriceProtectionContract[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [reload, setReload] = useState(0);
  const [statusFilter, setStatusFilter] = useState<"" | ContractStatus>("");
  const [contractTypeFilter, setContractTypeFilter] = useState<string>("");
  // Market price for the variance column, dollars per gallon (default $3.50).
  const [marketPrice, setMarketPrice] = useState<number | null>(3.5);
  // Dialog: null = closed, "new" = create, otherwise the contract to edit.
  const [editing, setEditing] = useState<
    null | "new" | PriceProtectionContract
  >(null);

  const fetchContracts = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: { status?: ContractStatus; page: number; size: number } = {
        page,
        size: 20,
      };
      if (statusFilter) filters.status = statusFilter;
      const response = await getPriceProtectionContracts(filters);
      setContracts(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load contracts");
    } finally {
      setLoading(false);
    }
    // `reload` refetches after a save.
  }, [page, statusFilter, reload]);

  useEffect(() => {
    fetchContracts();
  }, [fetchContracts]);

  // Contract type filters on the client (the API has no type filter).
  const filteredContracts = contractTypeFilter
    ? contracts.filter((c) => c.contract_type === contractTypeFilter)
    : contracts;
  const marketPriceCents = Math.round((marketPrice ?? 0) * 100);

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setEditing("new")}
      >
        Add Contract
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  const columns: Column<PriceProtectionContract>[] = [
    {
      key: "customer_id",
      header: "Customer",
      width: 170,
      // The contract's subject is its customer, navigable to the Commerce
      // module (Req 11.3, 13.1).
      cell: (c) => (
        <EntityLink
          type="customer"
          id={c.customer_id}
          className="font-medium"
          stopPropagation
        />
      ),
    },
    {
      key: "product_code",
      header: "Product",
      width: 200,
      cell: (c) => <ProductChip code={c.product_code} />,
    },
    {
      key: "contract_type",
      header: "Type",
      width: 110,
      cell: (c) => contractTypeLabel(c.contract_type),
    },
    {
      key: "terms",
      header: "Price",
      width: 140,
      className: "tabular-nums",
      cell: priceTerms,
    },
    {
      key: "period",
      header: "Period",
      width: 250,
      cell: (c) => `${formatDate(c.start_date)} – ${formatDate(c.end_date)}`,
    },
    {
      key: "remaining_gallons",
      header: "Remaining",
      align: "right",
      width: 170,
      className: "tabular-nums",
      cell: (c) =>
        `${gallons(c.remaining_gallons)} of ${gallons(c.contracted_gallons)}`,
    },
    {
      key: "status",
      header: "Status",
      width: 120,
      cell: (c) => {
        const st = CONTRACT_STATUS[c.status] ?? {
          status: "draft" as StatusKey,
          label: String(c.status),
        };
        return <StatusBadge status={st.status} label={st.label} />;
      },
    },
    {
      key: "settlement_variance",
      header: "Settlement variance",
      width: 210,
      cell: (c) => renderVarianceCell(c, marketPriceCents),
    },
  ];

  const popoverCount =
    (contractTypeFilter ? 1 : 0) + (marketPrice !== 3.5 ? 1 : 0);

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Price Protection Contracts
          </PageTitle>
          <div className="ml-auto">{actions}</div>
        </div>
      )}
      <Toolbar
        label="Contracts"
        filters={
          <>
            <FilterChips
              label="Contract status"
              options={STATUS_CHIPS.map((c) => ({
                id: c.id || "all",
                label: c.label,
                status: c.id ? CONTRACT_STATUS[c.id].status : undefined,
              }))}
              value={statusFilter || "all"}
              onChange={(v) => {
                setStatusFilter(v === "all" ? "" : (v as ContractStatus));
                setPage(1);
              }}
            />
            <FilterPopover
              count={popoverCount}
              label="Contract filters"
              onClear={() => {
                setContractTypeFilter("");
                setMarketPrice(3.5);
              }}
            >
              <div className="grid w-80 grid-cols-2 gap-3">
                <Field label="Contract type" id="contract-type-filter">
                  <Select
                    id="contract-type-filter"
                    value={contractTypeFilter}
                    onChange={setContractTypeFilter}
                    placeholder="All"
                    options={CONTRACT_TYPES}
                  />
                </Field>
                <Field
                  label="Market price"
                  id="market-price-input"
                  help="For the variance column"
                >
                  <NumberField
                    id="market-price-input"
                    value={marketPrice}
                    onChange={setMarketPrice}
                    unit="$"
                    decimals={2}
                    min={0}
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
        <DataTable<PriceProtectionContract>
          ariaLabel="Price protection contracts"
          columns={columns}
          data={loading || error ? [] : filteredContracts}
          loading={loading}
          error={error ? { message: error, onRetry: fetchContracts } : null}
          getRowId={(c) => c.contract_id}
          rowLabel={(c) => `Contract ${c.contract_id}`}
          onRowClick={(c) => setEditing(c)}
          rowMenu={(c) => [
            {
              id: "edit",
              label: "Edit",
              icon: <Pencil className="h-3.5 w-3.5" />,
              onSelect: () => setEditing(c),
            },
          ]}
          pagination={
            totalPages > 1
              ? { page, totalPages, onPageChange: setPage }
              : undefined
          }
          emptyState={
            <span className="text-text-muted">No contracts found.</span>
          }
        />
      </div>

      {editing && (
        <ContractDialog
          contract={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => setReload((n) => n + 1)}
        />
      )}
    </div>
  );
}

// ─── Account picker ──────────────────────────────────────────────────────────

/**
 * AccountSelect — searchable account selector scoped to a customer, backed by
 * /commerce/accounts. Accounts belong to a customer, so the roster is filtered
 * by the selected customer. Uses the generic SearchableSelect since there is
 * no dedicated account picker.
 */
function AccountSelect({
  id,
  customerId,
  value,
  onChange,
}: {
  id?: string;
  customerId: string;
  value: string | null;
  onChange: (accountId: string) => void;
}) {
  const [options, setOptions] = useState<SearchableSelectOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    if (!customerId) {
      setOptions([]);
      return;
    }
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError(false);
      try {
        const res = await getAccounts({
          customer_id: customerId,
          status: "active",
          limit: 200,
        });
        if (cancelled) return;
        const rows = Array.isArray(res.data) ? res.data : [];
        setOptions(
          rows.map((a) => ({
            value: a.account_id,
            label: a.display_name || a.account_id,
            sublabel: a.account_id,
          })),
        );
      } catch {
        if (!cancelled) setLoadError(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [customerId]);

  const mergedOptions =
    value && !options.some((o) => o.value === value)
      ? [{ value, label: value }, ...options]
      : options;

  return (
    <SearchableSelect
      id={id}
      aria-label="Account ID"
      options={mergedOptions}
      value={value}
      onChange={onChange}
      loading={loading}
      disabled={!customerId}
      placeholder={
        customerId ? "Select an account…" : "Select a customer first"
      }
      searchPlaceholder="Search accounts…"
      emptyMessage={loadError ? "Couldn't load accounts" : "No accounts found"}
    />
  );
}

// ─── Contract dialog ─────────────────────────────────────────────────────────

type ContractValues = {
  customer_id: string;
  account_id: string;
  product_code: string;
  contract_type: ContractType;
  start_date: string;
  end_date: string;
  contracted_gallons: number | null;
  /** Dollars per gallon (stored as cents). */
  price_cap: number | null;
  price_floor: number | null;
  fixed_price: number | null;
  status: ContractStatus;
};

const toDollars = (cents: number | null | undefined) =>
  cents == null ? null : cents / 100;
const toCents = (dollars: number | null) =>
  dollars == null ? null : Math.round(dollars * 100);

export function validateContract(v: ContractValues, isEdit: boolean) {
  const errors: Record<string, string | undefined> = {};
  if (!isEdit) {
    if (!v.customer_id) errors.customer_id = "Pick a customer.";
    if (!v.account_id) errors.account_id = "Pick an account.";
    if (!v.product_code) errors.product_code = "Pick a product.";
    if (!v.start_date) errors.start_date = "Enter the start date.";
    if (!v.end_date) errors.end_date = "Enter the end date.";
    if (v.contracted_gallons == null || v.contracted_gallons <= 0)
      errors.contracted_gallons = "Enter the contracted gallons.";
  }
  if (v.start_date && v.end_date && v.end_date <= v.start_date)
    errors.end_date = "End date must be after the start date.";
  if (v.contract_type === "fixed_price" && v.fixed_price == null)
    errors.fixed_price = "Enter the fixed price.";
  if (
    (v.contract_type === "cap_price" || v.contract_type === "collar") &&
    v.price_cap == null
  )
    errors.price_cap = "Enter the price cap.";
  if (v.contract_type === "collar") {
    if (v.price_floor == null) errors.price_floor = "Enter the price floor.";
    else if (v.price_cap != null && v.price_floor > v.price_cap)
      errors.price_floor = "Floor must not be above the cap.";
  }
  return errors;
}

function ContractDialog({
  contract,
  onClose,
  onSaved,
}: {
  contract: PriceProtectionContract | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const isEdit = contract !== null;
  const submit = async (v: ContractValues) => {
    if (contract) {
      const data: UpdatePriceProtectionContractPayload = {
        end_date: v.end_date || undefined,
        price_cap_cents: toCents(v.price_cap),
        price_floor_cents: toCents(v.price_floor),
        fixed_price_cents: toCents(v.fixed_price),
        status: v.status,
      };
      await updatePriceProtectionContract(contract.contract_id, data);
    } else {
      const data: CreatePriceProtectionContractPayload = {
        customer_id: v.customer_id,
        account_id: v.account_id,
        product_code: v.product_code,
        contract_type: v.contract_type,
        start_date: v.start_date,
        end_date: v.end_date,
        contracted_gallons: v.contracted_gallons ?? 0,
        price_cap_cents: toCents(v.price_cap),
        price_floor_cents: toCents(v.price_floor),
        fixed_price_cents: toCents(v.fixed_price),
      };
      await createPriceProtectionContract(data);
    }
  };
  return (
    <FormDialog<ContractValues, void>
      open
      size="lg"
      title={isEdit ? "Edit contract" : "Add contract"}
      submitLabel={isEdit ? "Update Contract" : "Add Contract"}
      successMessage={isEdit ? "Contract updated" : "Contract added"}
      initialValues={{
        customer_id: contract?.customer_id ?? "",
        account_id: contract?.account_id ?? "",
        product_code: contract?.product_code ?? "",
        contract_type: contract?.contract_type ?? "fixed_price",
        start_date: contract?.start_date ?? "",
        end_date: contract?.end_date ?? "",
        contracted_gallons: contract?.contracted_gallons ?? null,
        price_cap: toDollars(contract?.price_cap_cents),
        price_floor: toDollars(contract?.price_floor_cents),
        fixed_price: toDollars(contract?.fixed_price_cents),
        status: contract?.status ?? "active",
      }}
      validate={(v) => validateContract(v, isEdit)}
      onSubmit={submit}
      onSaved={onSaved}
      onClose={onClose}
    >
      {({ values, set, setValues, errors }) => (
        <>
          {isEdit && contract ? (
            <dl className="col-span-2 grid grid-cols-2 gap-3 rounded-lg border border-slate-200 p-3 text-sm md:grid-cols-4">
              <div>
                <dt className="text-xs text-text-muted">Customer</dt>
                <dd>
                  <EntityLink type="customer" id={contract.customer_id} />
                </dd>
              </div>
              <div>
                <dt className="text-xs text-text-muted">Product</dt>
                <dd>
                  <ProductChip code={contract.product_code} />
                </dd>
              </div>
              <div>
                <dt className="text-xs text-text-muted">Type</dt>
                <dd>{contractTypeLabel(contract.contract_type)}</dd>
              </div>
              <div>
                <dt className="text-xs text-text-muted">Contracted</dt>
                <dd className="tabular-nums">
                  {gallons(contract.contracted_gallons)}
                </dd>
              </div>
            </dl>
          ) : (
            <>
              <Field
                label="Customer ID"
                required
                span={1}
                error={errors.customer_id}
              >
                <CustomerPicker
                  id="customer-id"
                  aria-label="Customer ID"
                  value={values.customer_id || null}
                  onChange={(value) =>
                    // Account is scoped to the customer; clear it on change.
                    setValues((prev) => ({
                      ...prev,
                      customer_id: value,
                      account_id: "",
                    }))
                  }
                  allowClear
                />
              </Field>
              <Field
                label="Account ID"
                required
                span={1}
                error={errors.account_id}
              >
                <AccountSelect
                  id="account-id"
                  customerId={values.customer_id}
                  value={values.account_id || null}
                  onChange={(v) => set("account_id", v)}
                />
              </Field>
              <Field
                label="Product"
                required
                span={1}
                error={errors.product_code}
              >
                <ProductPicker
                  id="product-code"
                  aria-label="Product Code"
                  value={values.product_code || null}
                  onChange={(v) => set("product_code", v)}
                  allowClear
                />
              </Field>
              <Field label="Contract type" span={1}>
                <Select
                  id="contract-type"
                  value={values.contract_type}
                  onChange={(v) => set("contract_type", v as ContractType)}
                  options={CONTRACT_TYPES}
                />
              </Field>
              <Field
                label="Start Date"
                required
                span={1}
                error={errors.start_date}
              >
                <input
                  id="start-date"
                  type="date"
                  value={values.start_date}
                  onChange={(e) => set("start_date", e.target.value)}
                  className={INPUT_CLASS}
                />
              </Field>
            </>
          )}
          <Field
            label="End Date"
            required={!isEdit}
            span={1}
            error={errors.end_date}
          >
            <input
              id="end-date"
              type="date"
              value={values.end_date}
              onChange={(e) => set("end_date", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
          {!isEdit && (
            <Field
              label="Contracted Gallons"
              required
              span={1}
              error={errors.contracted_gallons}
            >
              <NumberField
                id="contracted-gallons"
                value={values.contracted_gallons}
                onChange={(n) => set("contracted_gallons", n)}
                unit="gal"
                decimals={0}
                min={0}
              />
            </Field>
          )}
          {values.contract_type === "fixed_price" && (
            <Field
              label="Fixed price"
              required
              span={1}
              help="Per gallon"
              error={errors.fixed_price}
            >
              <NumberField
                id="fixed-price"
                value={values.fixed_price}
                onChange={(n) => set("fixed_price", n)}
                unit="$"
                decimals={2}
                min={0}
              />
            </Field>
          )}
          {(values.contract_type === "cap_price" ||
            values.contract_type === "collar") && (
            <Field
              label="Price cap"
              required
              span={1}
              help="Per gallon"
              error={errors.price_cap}
            >
              <NumberField
                id="price-cap"
                value={values.price_cap}
                onChange={(n) => set("price_cap", n)}
                unit="$"
                decimals={2}
                min={0}
              />
            </Field>
          )}
          {values.contract_type === "collar" && (
            <Field
              label="Price floor"
              required
              span={1}
              help="Per gallon"
              error={errors.price_floor}
            >
              <NumberField
                id="price-floor"
                value={values.price_floor}
                onChange={(n) => set("price_floor", n)}
                unit="$"
                decimals={2}
                min={0}
              />
            </Field>
          )}
          {isEdit && (
            <Field label="Status" span={1}>
              <Select
                id="contract-status"
                value={values.status}
                onChange={(v) => set("status", v as ContractStatus)}
                options={(Object.keys(CONTRACT_STATUS) as ContractStatus[]).map(
                  (k) => ({ value: k, label: CONTRACT_STATUS[k].label }),
                )}
              />
            </Field>
          )}
        </>
      )}
    </FormDialog>
  );
}
