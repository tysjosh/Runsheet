"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  getPriceProtectionContracts,
  createPriceProtectionContract,
  updatePriceProtectionContract,
  type PriceProtectionContract,
  type ContractType,
  type ContractStatus,
  type CreatePriceProtectionContractPayload,
  type UpdatePriceProtectionContractPayload,
} from "../../services/complianceApi";

// ─── Sub-view types ──────────────────────────────────────────────────────────

type ViewMode = "list" | "add" | "edit";

// ─── Badge helpers ───────────────────────────────────────────────────────────

function statusBadge(status: ContractStatus) {
  switch (status) {
    case "active":
      return "bg-green-100 text-green-800";
    case "exhausted":
      return "bg-yellow-100 text-yellow-800";
    case "expired":
      return "bg-red-100 text-red-800";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

function contractTypeBadge(type: ContractType) {
  switch (type) {
    case "fixed_price":
      return "bg-blue-100 text-blue-800";
    case "cap_price":
      return "bg-purple-100 text-purple-800";
    case "collar":
      return "bg-indigo-100 text-indigo-800";
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

function formatCents(cents: number | null): string {
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
        Math.min(marketPriceCents, contract.price_cap_cents ?? marketPriceCents),
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
      setError(
        err instanceof Error ? err.message : "Failed to load contracts",
      );
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

  // ─── Render: Settlement Variance Display ─────────────────────────────────

  function renderVarianceCell(contract: PriceProtectionContract) {
    const variance = computeSettlementVariance(contract, marketPriceCents);
    if (variance.gallonsDelivered === 0) {
      return <span className="text-gray-400">No deliveries</span>;
    }

    const varianceDollars = variance.varianceCents / 100;
    const isPositive = varianceDollars >= 0;

    return (
      <div className="text-sm">
        <span
          className={`font-medium ${isPositive ? "text-green-700" : "text-red-700"}`}
        >
          {isPositive ? "+" : ""}${varianceDollars.toFixed(2)}
        </span>
        <span className="text-gray-400 ml-1 text-xs">
          ({formatGallons(variance.gallonsDelivered)} gal)
        </span>
      </div>
    );
  }

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
              onChange={(e) =>
                setMarketPriceCents(Number(e.target.value) || 0)
              }
              className="border rounded px-3 py-2 w-32"
              placeholder="350"
            />
          </div>
        </div>

        {/* Loading state */}
        {loading && (
          <div role="status" className="flex justify-center py-12">
            <span className="sr-only">Loading contracts...</span>
            <div className="animate-spin h-8 w-8 border-4 border-blue-600 border-t-transparent rounded-full" />
          </div>
        )}

        {/* Contracts table */}
        {!loading && !error && (
          <>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse" role="table">
                <thead>
                  <tr className="border-b bg-gray-50">
                    <th className="text-left p-3 font-medium">Customer ID</th>
                    <th className="text-left p-3 font-medium">Product</th>
                    <th className="text-left p-3 font-medium">Type</th>
                    <th className="text-left p-3 font-medium">Start Date</th>
                    <th className="text-left p-3 font-medium">End Date</th>
                    <th className="text-left p-3 font-medium">
                      Contracted Gal
                    </th>
                    <th className="text-left p-3 font-medium">
                      Remaining Gal
                    </th>
                    <th className="text-left p-3 font-medium">Status</th>
                    <th className="text-left p-3 font-medium">
                      Settlement Variance
                    </th>
                    <th className="text-left p-3 font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredContracts.map((contract) => (
                    <tr
                      key={contract.contract_id}
                      className="border-b hover:bg-gray-50"
                    >
                      <td className="p-3 font-medium">
                        {contract.customer_id}
                      </td>
                      <td className="p-3">{contract.product_code}</td>
                      <td className="p-3">
                        <span
                          className={`inline-block px-2 py-1 rounded text-xs font-medium ${contractTypeBadge(contract.contract_type)}`}
                        >
                          {contractTypeLabel(contract.contract_type)}
                        </span>
                      </td>
                      <td className="p-3">
                        {formatDate(contract.start_date)}
                      </td>
                      <td className="p-3">{formatDate(contract.end_date)}</td>
                      <td className="p-3">
                        {formatGallons(contract.contracted_gallons)}
                      </td>
                      <td className="p-3">
                        {formatGallons(contract.remaining_gallons)}
                      </td>
                      <td className="p-3">
                        <span
                          className={`inline-block px-2 py-1 rounded text-xs font-medium ${statusBadge(contract.status)}`}
                        >
                          {contract.status}
                        </span>
                      </td>
                      <td className="p-3">{renderVarianceCell(contract)}</td>
                      <td className="p-3">
                        <button
                          type="button"
                          onClick={() => handleEditContract(contract)}
                          className="text-blue-600 hover:underline text-sm"
                        >
                          Edit
                        </button>
                      </td>
                    </tr>
                  ))}
                  {filteredContracts.length === 0 && (
                    <tr>
                      <td
                        colSpan={10}
                        className="p-6 text-center text-gray-500"
                      >
                        No contracts found.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

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
                className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700"
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
          className="bg-red-50 border border-red-200 text-red-700 p-4 rounded mb-4"
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

interface ContractFormProps {
  initialData: PriceProtectionContract | null;
  onSubmit: (
    data: CreatePriceProtectionContractPayload | UpdatePriceProtectionContractPayload,
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
            <input
              id="customer-id"
              type="text"
              required
              value={customerId}
              onChange={(e) => setCustomerId(e.target.value)}
              className="w-full border rounded px-3 py-2"
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
            <input
              id="account-id"
              type="text"
              required
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className="w-full border rounded px-3 py-2"
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
            <input
              id="product-code"
              type="text"
              required
              value={productCode}
              onChange={(e) => setProductCode(e.target.value)}
              className="w-full border rounded px-3 py-2"
              placeholder="e.g. HEATING_OIL"
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
          <label
            htmlFor="end-date"
            className="block text-sm font-medium mb-1"
          >
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
        {(contractType === "cap_price" || contractType === "collar" || isEdit) && (
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
          className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700 disabled:opacity-50"
        >
          {loading
            ? "Saving..."
            : isEdit
              ? "Update Contract"
              : "Add Contract"}
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
