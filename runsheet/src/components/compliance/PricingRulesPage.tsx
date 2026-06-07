"use client";

import type React from "react";
import { useCallback, useEffect, useState } from "react";
import { type Column, EntityLink, Table } from "@/components/ui";
import {
  type CreatePricingRulePayload,
  createPricingRule,
  getPricingRules,
  type PriceResolution,
  type PricingRule,
  type PricingStrategy,
  type ResolvePricePayload,
  resolvePrice,
  type TierBreak,
} from "../../services/complianceApi";
import CustomerPicker from "../ops/CustomerPicker";
import ProductPicker from "../ops/ProductPicker";

// ─── Sub-view types ──────────────────────────────────────────────────────────

type ViewMode = "list" | "add";

// ─── Badge helpers ───────────────────────────────────────────────────────────

function strategyBadgeClass(strategy: PricingStrategy): string {
  switch (strategy) {
    case "posted_price":
      return "bg-info-light text-info-dark";
    case "rack_plus_margin":
      return "bg-success-light text-success-dark";
    case "tiered_volume":
      return "bg-brand-secondary-soft text-brand-secondary";
    case "cost_plus":
      return "bg-warning-light text-warning-dark";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

function strategyLabel(strategy: PricingStrategy): string {
  switch (strategy) {
    case "posted_price":
      return "Posted Price";
    case "rack_plus_margin":
      return "Rack + Margin";
    case "tiered_volume":
      return "Tiered Volume";
    case "cost_plus":
      return "Cost Plus";
    default:
      return strategy;
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

// ─── Table columns ───────────────────────────────────────────────────────────

const pricingRuleColumns: Column<PricingRule>[] = [
  {
    key: "customer_id",
    label: "Customer ID",
    // A pricing rule's subject is its customer when scoped to one; a rule with
    // no customer is a product-level default. The scoped customer is navigable
    // to the Commerce module (Req 11.3, 13.1).
    render: (rule) =>
      rule.customer_id ? (
        <EntityLink
          type="customer"
          id={rule.customer_id}
          className="font-medium"
        />
      ) : (
        <span className="font-medium">Default</span>
      ),
  },
  {
    key: "product_code",
    label: "Product Code",
    render: (rule) => rule.product_code,
  },
  {
    key: "strategy",
    label: "Strategy",
    render: (rule) => (
      <span
        className={`inline-block px-2 py-1 rounded text-xs font-medium ${strategyBadgeClass(rule.strategy)}`}
      >
        {strategyLabel(rule.strategy)}
      </span>
    ),
  },
  {
    key: "margin_cents",
    label: "Margin (¢)",
    render: (rule) =>
      rule.margin_cents !== null ? `${rule.margin_cents}¢` : "—",
  },
  {
    key: "priority",
    label: "Priority",
    render: (rule) => rule.priority,
  },
  {
    key: "effective_date",
    label: "Effective Date",
    render: (rule) => formatDate(rule.effective_date),
  },
  {
    key: "expiry_date",
    label: "Expiry Date",
    render: (rule) => formatDate(rule.expiry_date),
  },
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function PricingRulesPage() {
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [rules, setRules] = useState<PricingRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);

  // Filters
  const [strategyFilter, setStrategyFilter] = useState<string>("");
  const [productCodeFilter, setProductCodeFilter] = useState<string>("");

  // ─── Fetch pricing rules ─────────────────────────────────────────────────

  const fetchRules = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: {
        strategy?: PricingStrategy;
        product_code?: string;
        page: number;
        size: number;
      } = {
        page,
        size: 20,
      };
      if (strategyFilter) filters.strategy = strategyFilter as PricingStrategy;
      if (productCodeFilter) filters.product_code = productCodeFilter;

      const response = await getPricingRules(filters);
      setRules(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load pricing rules",
      );
    } finally {
      setLoading(false);
    }
  }, [page, strategyFilter, productCodeFilter]);

  useEffect(() => {
    if (viewMode === "list") {
      fetchRules();
    }
  }, [fetchRules, viewMode]);

  // ─── Render: Listing View ────────────────────────────────────────────────

  function renderList() {
    return (
      <>
        {/* Filters */}
        <div className="flex flex-wrap gap-4 mb-6 items-end">
          <div>
            <label
              htmlFor="strategy-filter"
              className="block text-sm font-medium mb-1"
            >
              Strategy
            </label>
            <select
              id="strategy-filter"
              value={strategyFilter}
              onChange={(e) => {
                setStrategyFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2"
            >
              <option value="">All</option>
              <option value="posted_price">Posted Price</option>
              <option value="rack_plus_margin">Rack + Margin</option>
              <option value="tiered_volume">Tiered Volume</option>
              <option value="cost_plus">Cost Plus</option>
            </select>
          </div>
          <div>
            <label
              htmlFor="product-code-filter"
              className="block text-sm font-medium mb-1"
            >
              Product Code
            </label>
            <input
              id="product-code-filter"
              type="text"
              value={productCodeFilter}
              onChange={(e) => {
                setProductCodeFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2 w-48"
              placeholder="e.g. ULSD"
            />
          </div>
        </div>

        {/* Loading state */}
        {loading && (
          <div role="status" className="flex justify-center py-12">
            <span className="sr-only">Loading pricing rules...</span>
            <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
          </div>
        )}

        {/* Error state */}
        {!loading && error && (
          <div
            role="alert"
            className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
          >
            {error}
          </div>
        )}

        {/* Rules table */}
        {!loading && !error && (
          <>
            <Table<PricingRule>
              ariaLabel="Sales pricing rules"
              columns={pricingRuleColumns}
              data={rules}
              getRowId={(rule) => rule.rule_id}
              emptyState={
                <span className="text-gray-500">No pricing rules found.</span>
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

        {/* Resolve Price Test Panel */}
        <ResolvePricePanel />
      </>
    );
  }

  // ─── Render: Add Form ────────────────────────────────────────────────────

  function renderForm() {
    return (
      <PricingRuleForm
        onSubmit={async (data) => {
          setLoading(true);
          setError(null);
          try {
            await createPricingRule(data);
            setViewMode("list");
          } catch (err) {
            setError(
              err instanceof Error ? err.message : "Failed to create rule",
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
            <h1 className="text-2xl font-bold">Sales Pricing Rules</h1>
            <p className="text-gray-600 mt-1">
              Manage pricing strategies for customers and products. Test price
              resolution with the panel below.
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
                Add Rule
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Error state (top-level) */}
      {error && viewMode === "list" && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* View content */}
      {viewMode === "list" && renderList()}
      {viewMode === "add" && renderForm()}
    </div>
  );
}

// ─── Pricing Rule Form Sub-Component ─────────────────────────────────────────

interface PricingRuleFormProps {
  onSubmit: (data: CreatePricingRulePayload) => Promise<void>;
  onCancel: () => void;
  loading: boolean;
}

function PricingRuleForm({
  onSubmit,
  onCancel,
  loading,
}: PricingRuleFormProps) {
  const [customerId, setCustomerId] = useState("");
  const [productCode, setProductCode] = useState("");
  const [strategy, setStrategy] = useState<PricingStrategy>("posted_price");
  const [priority, setPriority] = useState<string>("10");
  const [effectiveDate, setEffectiveDate] = useState("");
  const [expiryDate, setExpiryDate] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);

  // Strategy-specific fields
  const [postedPriceCents, setPostedPriceCents] = useState<string>("");
  const [marginCents, setMarginCents] = useState<string>("");
  const [freightRateCentsPerMile, setFreightRateCentsPerMile] =
    useState<string>("");
  const [tierThresholds, setTierThresholds] = useState<TierBreak[]>([
    { min_gallons: 0, max_gallons: 1000, price_cents: 350 },
  ]);

  // ─── Tier management ─────────────────────────────────────────────────────

  function addTier() {
    const lastTier = tierThresholds[tierThresholds.length - 1];
    setTierThresholds([
      ...tierThresholds,
      {
        min_gallons: lastTier ? (lastTier.max_gallons ?? 0) + 1 : 0,
        max_gallons: null,
        price_cents: 300,
      },
    ]);
  }

  function removeTier(index: number) {
    setTierThresholds(tierThresholds.filter((_, i) => i !== index));
  }

  function updateTier(index: number, field: keyof TierBreak, value: string) {
    const updated = [...tierThresholds];
    if (field === "max_gallons") {
      updated[index] = {
        ...updated[index],
        max_gallons: value === "" ? null : Number(value),
      };
    } else {
      updated[index] = { ...updated[index], [field]: Number(value) };
    }
    setTierThresholds(updated);
  }

  // ─── Submit handler ──────────────────────────────────────────────────────

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // Product code is required (previously enforced by the native input).
    if (!productCode) {
      setValidationError("Product code is required");
      return;
    }
    setValidationError(null);

    const data: CreatePricingRulePayload = {
      customer_id: customerId || null,
      product_code: productCode,
      strategy,
      priority: Number(priority),
      effective_date: effectiveDate,
      expiry_date: expiryDate || null,
    };

    // Attach strategy-specific fields
    switch (strategy) {
      case "posted_price":
        data.posted_price_cents = postedPriceCents
          ? Number(postedPriceCents)
          : null;
        break;
      case "rack_plus_margin":
        data.margin_cents = marginCents ? Number(marginCents) : null;
        break;
      case "tiered_volume":
        data.tier_thresholds = tierThresholds;
        break;
      case "cost_plus":
        data.margin_cents = marginCents ? Number(marginCents) : null;
        data.freight_rate_cents_per_mile = freightRateCentsPerMile
          ? Number(freightRateCentsPerMile)
          : null;
        break;
    }

    await onSubmit(data);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white border border-gray-200 rounded-lg p-6 shadow-sm max-w-2xl"
    >
      <h2 className="text-lg font-bold mb-4">Add New Pricing Rule</h2>

      {validationError && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-3 rounded mb-4 text-sm"
        >
          {validationError}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Customer ID (optional) */}
        <div>
          <label
            htmlFor="rule-customer-id"
            className="block text-sm font-medium mb-1"
          >
            Customer ID (optional)
          </label>
          <CustomerPicker
            id="rule-customer-id"
            aria-label="Customer ID (optional)"
            value={customerId || null}
            onChange={setCustomerId}
            allowClear
            placeholder="Leave blank for product default"
          />
        </div>

        {/* Product Code */}
        <div>
          <label
            htmlFor="rule-product-code"
            className="block text-sm font-medium mb-1"
          >
            Product Code
          </label>
          <ProductPicker
            id="rule-product-code"
            aria-label="Product Code"
            value={productCode || null}
            onChange={setProductCode}
            allowClear
          />
        </div>

        {/* Strategy */}
        <div>
          <label
            htmlFor="rule-strategy"
            className="block text-sm font-medium mb-1"
          >
            Strategy
          </label>
          <select
            id="rule-strategy"
            value={strategy}
            onChange={(e) => setStrategy(e.target.value as PricingStrategy)}
            className="w-full border rounded px-3 py-2"
          >
            <option value="posted_price">Posted Price</option>
            <option value="rack_plus_margin">Rack + Margin</option>
            <option value="tiered_volume">Tiered Volume</option>
            <option value="cost_plus">Cost Plus</option>
          </select>
        </div>

        {/* Priority */}
        <div>
          <label
            htmlFor="rule-priority"
            className="block text-sm font-medium mb-1"
          >
            Priority (lower = higher)
          </label>
          <input
            id="rule-priority"
            type="number"
            required
            min={1}
            value={priority}
            onChange={(e) => setPriority(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>

        {/* Effective Date */}
        <div>
          <label
            htmlFor="rule-effective-date"
            className="block text-sm font-medium mb-1"
          >
            Effective Date
          </label>
          <input
            id="rule-effective-date"
            type="date"
            required
            value={effectiveDate}
            onChange={(e) => setEffectiveDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>

        {/* Expiry Date */}
        <div>
          <label
            htmlFor="rule-expiry-date"
            className="block text-sm font-medium mb-1"
          >
            Expiry Date (optional)
          </label>
          <input
            id="rule-expiry-date"
            type="date"
            value={expiryDate}
            onChange={(e) => setExpiryDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
      </div>

      {/* Strategy-specific fields */}
      <div className="mt-6 border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-700 mb-3">
          Strategy Configuration — {strategyLabel(strategy)}
        </h3>

        {/* Posted Price: posted_price_cents */}
        {strategy === "posted_price" && (
          <div className="max-w-xs">
            <label
              htmlFor="rule-posted-price"
              className="block text-sm font-medium mb-1"
            >
              Posted Price (¢/gal)
            </label>
            <input
              id="rule-posted-price"
              type="number"
              min={0}
              required
              value={postedPriceCents}
              onChange={(e) => setPostedPriceCents(e.target.value)}
              className="w-full border rounded px-3 py-2"
              placeholder="e.g. 350"
            />
          </div>
        )}

        {/* Rack + Margin: margin_cents */}
        {strategy === "rack_plus_margin" && (
          <div className="max-w-xs">
            <label
              htmlFor="rule-margin-rack"
              className="block text-sm font-medium mb-1"
            >
              Margin (¢/gal above rack)
            </label>
            <input
              id="rule-margin-rack"
              type="number"
              min={0}
              required
              value={marginCents}
              onChange={(e) => setMarginCents(e.target.value)}
              className="w-full border rounded px-3 py-2"
              placeholder="e.g. 15"
            />
          </div>
        )}

        {/* Tiered Volume: tier_thresholds */}
        {strategy === "tiered_volume" && (
          <div>
            <p className="text-sm text-gray-600 mb-2">
              Define volume tiers with price breaks. Leave max gallons empty for
              the final tier (unlimited).
            </p>
            <div className="space-y-3">
              {tierThresholds.map((tier, idx) => (
                <div
                  key={idx}
                  className="flex items-center gap-3 bg-gray-50 p-3 rounded"
                >
                  <div>
                    <label className="block text-xs text-gray-500">
                      Min Gal
                    </label>
                    <input
                      type="number"
                      min={0}
                      value={tier.min_gallons}
                      onChange={(e) =>
                        updateTier(idx, "min_gallons", e.target.value)
                      }
                      className="w-24 border rounded px-2 py-1 text-sm"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500">
                      Max Gal
                    </label>
                    <input
                      type="number"
                      min={0}
                      value={tier.max_gallons ?? ""}
                      onChange={(e) =>
                        updateTier(idx, "max_gallons", e.target.value)
                      }
                      className="w-24 border rounded px-2 py-1 text-sm"
                      placeholder="∞"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500">
                      Price (¢/gal)
                    </label>
                    <input
                      type="number"
                      min={0}
                      value={tier.price_cents}
                      onChange={(e) =>
                        updateTier(idx, "price_cents", e.target.value)
                      }
                      className="w-24 border rounded px-2 py-1 text-sm"
                    />
                  </div>
                  {tierThresholds.length > 1 && (
                    <button
                      type="button"
                      onClick={() => removeTier(idx)}
                      className="text-error hover:text-error-dark text-sm mt-4"
                    >
                      Remove
                    </button>
                  )}
                </div>
              ))}
            </div>
            <button
              type="button"
              onClick={addTier}
              className="mt-2 text-info hover:underline text-sm"
            >
              + Add Tier
            </button>
          </div>
        )}

        {/* Cost Plus: freight_rate_cents_per_mile + margin_cents */}
        {strategy === "cost_plus" && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 max-w-lg">
            <div>
              <label
                htmlFor="rule-freight-rate"
                className="block text-sm font-medium mb-1"
              >
                Freight Rate (¢/mile)
              </label>
              <input
                id="rule-freight-rate"
                type="number"
                min={0}
                required
                value={freightRateCentsPerMile}
                onChange={(e) => setFreightRateCentsPerMile(e.target.value)}
                className="w-full border rounded px-3 py-2"
                placeholder="e.g. 5"
              />
            </div>
            <div>
              <label
                htmlFor="rule-margin-cost"
                className="block text-sm font-medium mb-1"
              >
                Margin (¢/gal)
              </label>
              <input
                id="rule-margin-cost"
                type="number"
                min={0}
                required
                value={marginCents}
                onChange={(e) => setMarginCents(e.target.value)}
                className="w-full border rounded px-3 py-2"
                placeholder="e.g. 10"
              />
            </div>
          </div>
        )}
      </div>

      <div className="flex gap-3 mt-6">
        <button
          type="submit"
          disabled={loading}
          className="bg-primary text-white px-4 py-2 rounded hover:bg-primary-hover disabled:opacity-50"
        >
          {loading ? "Saving..." : "Add Rule"}
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

// ─── Resolve Price Test Panel ────────────────────────────────────────────────

function ResolvePricePanel() {
  const [customerId, setCustomerId] = useState("");
  const [productCode, setProductCode] = useState("");
  const [gallons, setGallons] = useState<string>("");
  const [terminalId, setTerminalId] = useState("");
  const [routeMiles, setRouteMiles] = useState<string>("");

  const [resolving, setResolving] = useState(false);
  const [result, setResult] = useState<PriceResolution | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);

  const handleResolve = async (e: React.FormEvent) => {
    e.preventDefault();
    setResolving(true);
    setResult(null);
    setResolveError(null);

    if (!customerId || !productCode) {
      setResolving(false);
      setResolveError("Customer and product code are required");
      return;
    }

    try {
      const payload: ResolvePricePayload = {
        customer_id: customerId,
        product_code: productCode,
        gallons: Number(gallons),
      };
      if (terminalId) payload.terminal_id = terminalId;
      if (routeMiles) payload.route_miles = Number(routeMiles);

      const response = await resolvePrice(payload);
      setResult(response.data);
    } catch (err) {
      setResolveError(
        err instanceof Error ? err.message : "Failed to resolve price",
      );
    } finally {
      setResolving(false);
    }
  };

  return (
    <div className="mt-8 border-t pt-6">
      <h2 className="text-lg font-bold mb-2">Resolve Price — Test Panel</h2>
      <p className="text-sm text-gray-600 mb-4">
        Test the pricing engine by entering customer, product, and volume to see
        the resolved price.
      </p>

      <form
        onSubmit={handleResolve}
        className="bg-gray-50 border border-gray-200 rounded-lg p-4 max-w-3xl"
      >
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label
              htmlFor="resolve-customer-id"
              className="block text-sm font-medium mb-1"
            >
              Customer ID
            </label>
            <CustomerPicker
              id="resolve-customer-id"
              aria-label="Customer ID"
              value={customerId || null}
              onChange={setCustomerId}
              allowClear
            />
          </div>
          <div>
            <label
              htmlFor="resolve-product-code"
              className="block text-sm font-medium mb-1"
            >
              Product Code
            </label>
            <ProductPicker
              id="resolve-product-code"
              aria-label="Product Code"
              value={productCode || null}
              onChange={setProductCode}
              allowClear
            />
          </div>
          <div>
            <label
              htmlFor="resolve-gallons"
              className="block text-sm font-medium mb-1"
            >
              Gallons
            </label>
            <input
              id="resolve-gallons"
              type="number"
              required
              min={0}
              step="0.1"
              value={gallons}
              onChange={(e) => setGallons(e.target.value)}
              className="w-full border rounded px-3 py-2"
              placeholder="500"
            />
          </div>
          <div>
            <label
              htmlFor="resolve-terminal-id"
              className="block text-sm font-medium mb-1"
            >
              Terminal ID (optional)
            </label>
            <input
              id="resolve-terminal-id"
              type="text"
              value={terminalId}
              onChange={(e) => setTerminalId(e.target.value)}
              className="w-full border rounded px-3 py-2"
              placeholder="TERM-01"
            />
          </div>
          <div>
            <label
              htmlFor="resolve-route-miles"
              className="block text-sm font-medium mb-1"
            >
              Route Miles (optional)
            </label>
            <input
              id="resolve-route-miles"
              type="number"
              min={0}
              step="0.1"
              value={routeMiles}
              onChange={(e) => setRouteMiles(e.target.value)}
              className="w-full border rounded px-3 py-2"
              placeholder="25"
            />
          </div>
          <div className="flex items-end">
            <button
              type="submit"
              disabled={resolving}
              className="bg-success text-white px-4 py-2 rounded hover:bg-success-dark disabled:opacity-50 w-full"
            >
              {resolving ? "Resolving..." : "Resolve Price"}
            </button>
          </div>
        </div>
      </form>

      {/* Resolve result */}
      {result && (
        <div className="mt-4 bg-white border border-success-light rounded-lg p-4 max-w-3xl">
          <h3 className="text-sm font-semibold text-success-dark mb-2">
            Price Resolved
          </h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <span className="text-gray-500 block">Resolved Price</span>
              <span className="font-bold text-lg">
                {formatCents(result.resolved_price_cents)}
              </span>
              <span className="text-gray-400 text-xs">/gal</span>
            </div>
            <div>
              <span className="text-gray-500 block">Strategy Used</span>
              <span
                className={`inline-block px-2 py-1 rounded text-xs font-medium ${strategyBadgeClass(result.strategy_used)}`}
              >
                {strategyLabel(result.strategy_used)}
              </span>
            </div>
            <div>
              <span className="text-gray-500 block">Rule ID</span>
              <span className="font-mono text-xs">{result.rule_id}</span>
            </div>
            <div>
              <span className="text-gray-500 block">Breakdown</span>
              <div className="text-xs text-gray-600">
                {Object.entries(result.breakdown).map(([key, value]) => (
                  <div key={key}>
                    {key}: {formatCents(value)}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Resolve error */}
      {resolveError && (
        <div
          role="alert"
          className="mt-4 bg-error-light border border-error-light text-error-dark p-4 rounded max-w-3xl"
        >
          {resolveError}
        </div>
      )}
    </div>
  );
}
