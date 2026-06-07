"use client";

import type React from "react";
import { useCallback, useEffect, useState } from "react";
import type { SearchableSelectOption } from "@/components/ui";
import {
  type Column,
  EntityLink,
  SearchableSelect,
  Table,
} from "@/components/ui";
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
import CustomerPicker from "../ops/CustomerPicker";
import ProductPicker from "../ops/ProductPicker";

// ─── Sub-view types ──────────────────────────────────────────────────────────

type ViewMode = "list" | "add" | "edit";

// ─── Badge helpers ───────────────────────────────────────────────────────────

function statusBadge(status: ContractStatus) {
  switch (status) {
    case "active":
      return "bg-success-light text-success-dark";
    case "exhausted":
      return "bg-warning-light text-warning-dark";
    case "expired":
      return "bg-error-light text-error-dark";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

function contractTypeBadge(type: ContractType) {
  switch (type) {
    case "fixed_price":
      return "bg-info-light text-info-dark";
    case "cap_price":
      return "bg-brand-secondary-soft text-brand-secondary";
    case "collar":
      return "bg-brand-secondary-soft text-brand-secondary";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

function contractTypeLabel(type: ContractType): string {
  switch (type) {
    case "fixed_price":
      return "Fixed Price";
    case "cap_price":
      return "Cap Price";
    case "collar":
      return "Collar";
    default:
      return type;
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString();
}

function _formatCents(cents: number | null): string {
  if (cents === null || cents === undefined) return "—";
  return `$${(cents / 100).toFixed(2)}`;
}

function formatGallons(gallons: number): string {
  return gallons.toLocaleString(undefined, { maximumFractionDigits: 1 });
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
    return <span className="text-gray-500">No deliveries</span>;
  }

  const varianceDollars = variance.varianceCents / 100;
  const isPositive = varianceDollars >= 0;

  return (
    <div className="text-sm">
      <span
        className={`font-medium ${isPositive ? "text-success-dark" : "text-error-dark"}`}
      >
        {isPositive ? "+" : ""}${varianceDollars.toFixed(2)}
      </span>
      <span className="text-gray-500 ml-1 text-xs">
        ({formatGallons(variance.gallonsDelivered)} gal)
      </span>
    </div>
  );
}

// ─── Table columns ───────────────────────────────────────────────────────────

function getContractColumns(
  marketPriceCents: number,
  onEdit: (contract: PriceProtectionContract) => void,
): Column<PriceProtectionContract>[] {
  return [
    {
      key: "customer_id",
      label: "Customer ID",
      // The contract's subject is its customer, navigable to the Commerce
      // module (Req 11.3, 13.1).
      render: (contract) => (
        <EntityLink
          type="customer"
          id={contract.customer_id}
          className="font-medium"
        />
      ),
    },
    {
      key: "product_code",
      label: "Product",
      render: (contract) => contract.product_code,
    },
    {
      key: "contract_type",
      label: "Type",
      render: (contract) => (
        <span
          className={`inline-block px-2 py-1 rounded text-xs font-medium ${contractTypeBadge(contract.contract_type)}`}
        >
          {contractTypeLabel(contract.contract_type)}
        </span>
      ),
    },
    {
      key: "start_date",
      label: "Start Date",
      render: (contract) => formatDate(contract.start_date),
    },
    {
      key: "end_date",
      label: "End Date",
      render: (contract) => formatDate(contract.end_date),
    },
    {
      key: "contracted_gallons",
      label: "Contracted Gal",
      render: (contract) => formatGallons(contract.contracted_gallons),
    },
    {
      key: "remaining_gallons",
      label: "Remaining Gal",
      render: (contract) => formatGallons(contract.remaining_gallons),
    },
    {
      key: "status",
      label: "Status",
      render: (contract) => (
        <span
          className={`inline-block px-2 py-1 rounded text-xs font-medium ${statusBadge(contract.status)}`}
        >
          {contract.status}
        </span>
      ),
    },
    {
      key: "settlement_variance",
      label: "Settlement Variance",
      render: (contract) => renderVarianceCell(contract, marketPriceCents),
    },
    {
      key: "actions",
      label: "Actions",
      render: (contract) => (
        <button
          type="button"
          onClick={() => onEdit(contract)}
          className="text-info hover:underline text-sm"
        >
          Edit
        </button>
      ),
    },
  ];
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function PriceProtectionContractsPage() {
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [contracts, setContracts] = useState<PriceProtectionContract[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [contractTypeFilter, setContractTypeFilter] = useState<string>("");

  // Edit state
  const [editingContract, setEditingContract] =
    useState<PriceProtectionContract | null>(null);

  // Market price for variance display (configurable, default 350 cents = $3.50/gal)
  const [marketPriceCents, setMarketPriceCents] = useState<number>(350);

  // ─── Fetch contracts list ────────────────────────────────────────────────

  const fetchContracts = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: {
        status?: ContractStatus;
        page: number;
        size: number;
      } = {
        page,
        size: 20,
      };
      if (statusFilter) filters.status = statusFilter as ContractStatus;

      const response = await getPriceProtectionContracts(filters);
      setContracts(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load contracts");
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter]);

  useEffect(() => {
    if (viewMode === "list") {
      fetchContracts();
    }
  }, [fetchContracts, viewMode]);

  // ─── Edit contract ───────────────────────────────────────────────────────

  const handleEditContract = (contract: PriceProtectionContract) => {
    setEditingContract(contract);
    setViewMode("edit");
  };

  // ─── Filter contracts by type (client-side since API may not support it) ─

  const filteredContracts = contractTypeFilter
    ? contracts.filter((c) => c.contract_type === contractTypeFilter)
    : contracts;

  // ─── Render: Listing View ────────────────────────────────────────────────

  function renderList() {
    return (
      <>
        {/* Filters */}
        <div className="flex flex-wrap gap-4 mb-6 items-end">
          <div>
            <label
              htmlFor="contract-status-filter"
              className="block text-sm font-medium mb-1"
            >
              Status
            </label>
            <select
              id="contract-status-filter"
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2"
            >
              <option value="">All</option>
              <option value="active">Active</option>
              <option value="exhausted">Exhausted</option>
              <option value="expired">Expired</option>
            </select>
          </div>
          <div>
            <label
              htmlFor="contract-type-filter"
              className="block text-sm font-medium mb-1"
            >
              Contract Type
            </label>
            <select
              id="contract-type-filter"
              value={contractTypeFilter}
              onChange={(e) => setContractTypeFilter(e.target.value)}
              className="border rounded px-3 py-2"
            >
              <option value="">All</option>
              <option value="fixed_price">Fixed Price</option>
              <option value="cap_price">Cap Price</option>
              <option value="collar">Collar</option>
            </select>
          </div>
          <div>
            <label
              htmlFor="market-price-input"
              className="block text-sm font-medium mb-1"
            >
              Market Price (¢/gal)
            </label>
            <input
              id="market-price-input"
              type="number"
              min={0}
              value={marketPriceCents}
              onChange={(e) => setMarketPriceCents(Number(e.target.value) || 0)}
              className="border rounded px-3 py-2 w-32"
              placeholder="350"
            />
          </div>
        </div>

        {/* Loading state */}
        {loading && (
          <div role="status" className="flex justify-center py-12">
            <span className="sr-only">Loading contracts...</span>
            <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
          </div>
        )}

        {/* Contracts table */}
        {!loading && !error && (
          <>
            <Table<PriceProtectionContract>
              ariaLabel="Price protection contracts"
              columns={getContractColumns(marketPriceCents, handleEditContract)}
              data={filteredContracts}
              getRowId={(contract) => contract.contract_id}
              emptyState={
                <span className="text-gray-500">No contracts found.</span>
              }
            />

            {/* Pagination */}
            <nav
              aria-label="Pagination"
              className="flex justify-between items-center mt-4"
            >
              <button
                type="button"
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                className="px-3 py-1 border rounded disabled:opacity-50"
              >
                Previous
              </button>
              <span className="text-sm text-gray-600">
                Page {page} of {totalPages}
              </span>
              <button
                type="button"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
                className="px-3 py-1 border rounded disabled:opacity-50"
              >
                Next
              </button>
            </nav>
          </>
        )}
      </>
    );
  }

  // ─── Render: Add/Edit Form ───────────────────────────────────────────────

  function renderForm() {
    const isEdit = viewMode === "edit";
    return (
      <ContractForm
        initialData={isEdit ? editingContract : null}
        onSubmit={async (data) => {
          setLoading(true);
          setError(null);
          try {
            if (isEdit && editingContract) {
              await updatePriceProtectionContract(
                editingContract.contract_id,
                data as UpdatePriceProtectionContractPayload,
              );
            } else {
              await createPriceProtectionContract(
                data as CreatePriceProtectionContractPayload,
              );
            }
            setViewMode("list");
          } catch (err) {
            setError(
              err instanceof Error ? err.message : "Failed to save contract",
            );
          } finally {
            setLoading(false);
          }
        }}
        onCancel={() => setViewMode("list")}
        loading={loading}
      />
    );
  }

  // ─── Main Render ─────────────────────────────────────────────────────────

  return (
    <div className="p-6">
      <header className="mb-6">
        <div className="flex justify-between items-center">
          <div>
            <h1 className="text-2xl font-bold">Price Protection Contracts</h1>
            <p className="text-gray-600 mt-1">
              Manage sell-side price-protection contracts and track settlement
              variance.
            </p>
          </div>
          <div className="flex gap-2">
            {viewMode !== "list" && (
              <button
                type="button"
                onClick={() => setViewMode("list")}
                className="px-4 py-2 border rounded text-sm hover:bg-gray-50"
              >
                Back to List
              </button>
            )}
            {viewMode === "list" && (
              <button
                type="button"
                onClick={() => setViewMode("add")}
                className="bg-primary text-white px-4 py-2 rounded text-sm hover:bg-primary-hover"
              >
                Add Contract
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Error state */}
      {error && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* View content */}
      {viewMode === "list" && renderList()}
      {(viewMode === "add" || viewMode === "edit") && renderForm()}
    </div>
  );
}

// ─── Contract Form Sub-Component ─────────────────────────────────────────────

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

interface ContractFormProps {
  initialData: PriceProtectionContract | null;
  onSubmit: (
    data:
      | CreatePriceProtectionContractPayload
      | UpdatePriceProtectionContractPayload,
  ) => Promise<void>;
  onCancel: () => void;
  loading: boolean;
}

function ContractForm({
  initialData,
  onSubmit,
  onCancel,
  loading,
}: ContractFormProps) {
  const isEdit = !!initialData;

  const [customerId, setCustomerId] = useState(initialData?.customer_id ?? "");
  const [accountId, setAccountId] = useState(initialData?.account_id ?? "");
  const [productCode, setProductCode] = useState(
    initialData?.product_code ?? "",
  );
  const [contractType, setContractType] = useState<ContractType>(
    initialData?.contract_type ?? "fixed_price",
  );
  const [startDate, setStartDate] = useState(initialData?.start_date ?? "");
  const [endDate, setEndDate] = useState(initialData?.end_date ?? "");
  const [contractedGallons, setContractedGallons] = useState<string>(
    initialData?.contracted_gallons?.toString() ?? "",
  );
  const [priceCapCents, setPriceCapCents] = useState<string>(
    initialData?.price_cap_cents?.toString() ?? "",
  );
  const [priceFloorCents, setPriceFloorCents] = useState<string>(
    initialData?.price_floor_cents?.toString() ?? "",
  );
  const [fixedPriceCents, setFixedPriceCents] = useState<string>(
    initialData?.fixed_price_cents?.toString() ?? "",
  );
  const [status, setStatus] = useState<ContractStatus>(
    initialData?.status ?? "active",
  );
  const [validationError, setValidationError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (isEdit) {
      const data: UpdatePriceProtectionContractPayload = {
        end_date: endDate || undefined,
        price_cap_cents: priceCapCents ? Number(priceCapCents) : null,
        price_floor_cents: priceFloorCents ? Number(priceFloorCents) : null,
        fixed_price_cents: fixedPriceCents ? Number(fixedPriceCents) : null,
        status,
      };
      await onSubmit(data);
    } else {
      // Customer, account, and product are required on create (previously
      // enforced by native inputs now replaced with pickers).
      if (!customerId || !accountId || !productCode) {
        setValidationError("Customer, account, and product code are required");
        return;
      }
      setValidationError(null);
      const data: CreatePriceProtectionContractPayload = {
        customer_id: customerId,
        account_id: accountId,
        product_code: productCode,
        contract_type: contractType,
        start_date: startDate,
        end_date: endDate,
        contracted_gallons: Number(contractedGallons),
        price_cap_cents: priceCapCents ? Number(priceCapCents) : null,
        price_floor_cents: priceFloorCents ? Number(priceFloorCents) : null,
        fixed_price_cents: fixedPriceCents ? Number(fixedPriceCents) : null,
      };
      await onSubmit(data);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white border border-gray-200 rounded-lg p-6 shadow-sm max-w-2xl"
    >
      <h2 className="text-lg font-bold mb-4">
        {isEdit ? "Edit Contract" : "Add New Contract"}
      </h2>

      {validationError && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-3 rounded mb-4 text-sm"
        >
          {validationError}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Customer ID — only on create */}
        {!isEdit && (
          <div>
            <label
              htmlFor="customer-id"
              className="block text-sm font-medium mb-1"
            >
              Customer ID
            </label>
            <CustomerPicker
              id="customer-id"
              aria-label="Customer ID"
              value={customerId || null}
              onChange={(value) => {
                setCustomerId(value);
                // Account is scoped to the customer; clear it on change.
                setAccountId("");
              }}
              allowClear
            />
          </div>
        )}

        {/* Account ID — only on create */}
        {!isEdit && (
          <div>
            <label
              htmlFor="account-id"
              className="block text-sm font-medium mb-1"
            >
              Account ID
            </label>
            <AccountSelect
              id="account-id"
              customerId={customerId}
              value={accountId || null}
              onChange={setAccountId}
            />
          </div>
        )}

        {/* Product Code — only on create */}
        {!isEdit && (
          <div>
            <label
              htmlFor="product-code"
              className="block text-sm font-medium mb-1"
            >
              Product Code
            </label>
            <ProductPicker
              id="product-code"
              aria-label="Product Code"
              value={productCode || null}
              onChange={setProductCode}
              allowClear
            />
          </div>
        )}

        {/* Contract Type — only on create */}
        {!isEdit && (
          <div>
            <label
              htmlFor="contract-type"
              className="block text-sm font-medium mb-1"
            >
              Contract Type
            </label>
            <select
              id="contract-type"
              value={contractType}
              onChange={(e) => setContractType(e.target.value as ContractType)}
              className="w-full border rounded px-3 py-2"
            >
              <option value="fixed_price">Fixed Price</option>
              <option value="cap_price">Cap Price</option>
              <option value="collar">Collar</option>
            </select>
          </div>
        )}

        {/* Start Date — only on create */}
        {!isEdit && (
          <div>
            <label
              htmlFor="start-date"
              className="block text-sm font-medium mb-1"
            >
              Start Date
            </label>
            <input
              id="start-date"
              type="date"
              required
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="w-full border rounded px-3 py-2"
            />
          </div>
        )}

        {/* End Date */}
        <div>
          <label htmlFor="end-date" className="block text-sm font-medium mb-1">
            End Date
          </label>
          <input
            id="end-date"
            type="date"
            required={!isEdit}
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>

        {/* Contracted Gallons — only on create */}
        {!isEdit && (
          <div>
            <label
              htmlFor="contracted-gallons"
              className="block text-sm font-medium mb-1"
            >
              Contracted Gallons
            </label>
            <input
              id="contracted-gallons"
              type="number"
              required
              min={0}
              step="0.1"
              value={contractedGallons}
              onChange={(e) => setContractedGallons(e.target.value)}
              className="w-full border rounded px-3 py-2"
            />
          </div>
        )}

        {/* Price Cap (cents) — for cap_price and collar */}
        {(contractType === "cap_price" ||
          contractType === "collar" ||
          isEdit) && (
          <div>
            <label
              htmlFor="price-cap-cents"
              className="block text-sm font-medium mb-1"
            >
              Price Cap (¢/gal)
            </label>
            <input
              id="price-cap-cents"
              type="number"
              min={0}
              value={priceCapCents}
              onChange={(e) => setPriceCapCents(e.target.value)}
              className="w-full border rounded px-3 py-2"
              placeholder="e.g. 400"
            />
          </div>
        )}

        {/* Price Floor (cents) — for collar */}
        {(contractType === "collar" || isEdit) && (
          <div>
            <label
              htmlFor="price-floor-cents"
              className="block text-sm font-medium mb-1"
            >
              Price Floor (¢/gal)
            </label>
            <input
              id="price-floor-cents"
              type="number"
              min={0}
              value={priceFloorCents}
              onChange={(e) => setPriceFloorCents(e.target.value)}
              className="w-full border rounded px-3 py-2"
              placeholder="e.g. 300"
            />
          </div>
        )}

        {/* Fixed Price (cents) — for fixed_price */}
        {(contractType === "fixed_price" || isEdit) && (
          <div>
            <label
              htmlFor="fixed-price-cents"
              className="block text-sm font-medium mb-1"
            >
              Fixed Price (¢/gal)
            </label>
            <input
              id="fixed-price-cents"
              type="number"
              min={0}
              value={fixedPriceCents}
              onChange={(e) => setFixedPriceCents(e.target.value)}
              className="w-full border rounded px-3 py-2"
              placeholder="e.g. 350"
            />
          </div>
        )}

        {/* Status — only on edit */}
        {isEdit && (
          <div>
            <label
              htmlFor="contract-status"
              className="block text-sm font-medium mb-1"
            >
              Status
            </label>
            <select
              id="contract-status"
              value={status}
              onChange={(e) => setStatus(e.target.value as ContractStatus)}
              className="w-full border rounded px-3 py-2"
            >
              <option value="active">Active</option>
              <option value="exhausted">Exhausted</option>
              <option value="expired">Expired</option>
            </select>
          </div>
        )}
      </div>

      <div className="flex gap-3 mt-6">
        <button
          type="submit"
          disabled={loading}
          className="bg-primary text-white px-4 py-2 rounded hover:bg-primary-hover disabled:opacity-50"
        >
          {loading ? "Saving..." : isEdit ? "Update Contract" : "Add Contract"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 border rounded hover:bg-gray-50"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
