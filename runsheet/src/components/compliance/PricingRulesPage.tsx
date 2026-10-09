"use client";

/**
 * Billing → Pricing rules (UI revamp task 3.5): strategy and product in the
 * Filters popover, a DataTable with products by name and prices in dollars,
 * "Add rule" as an lg sectioned FormDialog with tier rows (design.md §5), and
 * the resolve-price calculator in a drawer (a dry run, not create/edit).
 * Prices are stored in cents; the dialog edits dollars with 2 decimals.
 */
import { Calculator, Plus, RefreshCw, Trash2 } from "lucide-react";
import type React from "react";
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
  FormSection,
  IconButton,
  INPUT_CLASS,
  NumberField,
  ProductChip,
  Select,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import {
  calendarDate,
  humanize,
  money,
  number,
  parseNumber,
} from "../../lib/format";
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
import { PageTitle } from "../ui/PageHeader";

const STRATEGIES: { value: PricingStrategy; label: string }[] = [
  { value: "posted_price", label: "Posted price" },
  { value: "rack_plus_margin", label: "Rack + margin" },
  { value: "tiered_volume", label: "Tiered volume" },
  { value: "cost_plus", label: "Cost plus" },
];

function strategyLabel(strategy: PricingStrategy): string {
  return STRATEGIES.find((s) => s.value === strategy)?.label ?? strategy;
}

function formatDate(dateStr: string | null): string {
  return dateStr ? calendarDate(dateStr) : "—";
}

function formatCents(cents: number | null): string {
  if (cents === null || cents === undefined) return "—";
  return money(cents / 100);
}

/** The rule's price terms in one cell. */
function ruleTerms(rule: PricingRule): string {
  switch (rule.strategy) {
    case "posted_price":
      return formatCents(rule.posted_price_cents ?? null);
    case "rack_plus_margin":
      return `Rack + ${formatCents(rule.margin_cents)}`;
    case "cost_plus":
      return `Cost + ${formatCents(rule.margin_cents)}`;
    case "tiered_volume":
      return `${number(rule.tier_thresholds?.length ?? 0)} tiers`;
    default:
      return "—";
  }
}

const pricingRuleColumns: Column<PricingRule>[] = [
  {
    key: "customer_id",
    header: "Customer",
    width: 180,
    // A pricing rule's subject is its customer when scoped to one; a rule with
    // no customer is a product-level default (Req 11.3, 13.1).
    cell: (rule) =>
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
    header: "Product",
    width: 200,
    cell: (rule) => <ProductChip code={rule.product_code} />,
  },
  {
    key: "strategy",
    header: "Strategy",
    width: 140,
    cell: (rule) => strategyLabel(rule.strategy),
  },
  {
    key: "terms",
    header: "Price",
    width: 150,
    className: "tabular-nums",
    cell: ruleTerms,
  },
  {
    key: "priority",
    header: "Priority",
    align: "right",
    width: 90,
    className: "tabular-nums",
    cell: (rule) => number(rule.priority),
  },
  {
    key: "effective_date",
    header: "Effective",
    width: 150,
    cell: (rule) => formatDate(rule.effective_date),
  },
  {
    key: "expiry_date",
    header: "Expires",
    width: 150,
    cell: (rule) => formatDate(rule.expiry_date),
  },
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function PricingRulesPage() {
  const [rules, setRules] = useState<PricingRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [reload, setReload] = useState(0);
  const [strategyFilter, setStrategyFilter] = useState<string>("");
  const [productCodeFilter, setProductCodeFilter] = useState<string>("");
  const [adding, setAdding] = useState(false);
  const [priceCheckOpen, setPriceCheckOpen] = useState(false);

  const fetchRules = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: {
        strategy?: PricingStrategy;
        product_code?: string;
        page: number;
        size: number;
      } = { page, size: 20 };
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
    // `reload` refetches after a create.
  }, [page, strategyFilter, productCodeFilter, reload]);

  useEffect(() => {
    fetchRules();
  }, [fetchRules]);

  const actions = useMemo(
    () => (
      <>
        <Button
          size="sm"
          variant="secondary"
          icon={<Calculator className="h-3.5 w-3.5" />}
          onClick={() => setPriceCheckOpen(true)}
        >
          Price check
        </Button>
        <Button
          size="sm"
          icon={<Plus className="h-3.5 w-3.5" />}
          onClick={() => setAdding(true)}
        >
          Add Rule
        </Button>
      </>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });
  const popoverCount = (strategyFilter ? 1 : 0) + (productCodeFilter ? 1 : 0);

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Sales Pricing Rules
          </PageTitle>
          <div className="ml-auto flex gap-2">{actions}</div>
        </div>
      )}
      <Toolbar
        label="Pricing rules"
        filters={
          <FilterPopover
            count={popoverCount}
            label="Pricing rule filters"
            onClear={() => {
              setStrategyFilter("");
              setProductCodeFilter("");
              setPage(1);
            }}
          >
            <div className="grid w-80 gap-3">
              <Field label="Strategy" id="strategy-filter">
                <Select
                  id="strategy-filter"
                  value={strategyFilter}
                  onChange={(v) => {
                    setStrategyFilter(v);
                    setPage(1);
                  }}
                  placeholder="All"
                  options={STRATEGIES}
                />
              </Field>
              <Field label="Product" id="product-code-filter">
                <ProductPicker
                  id="product-code-filter"
                  aria-label="Product Code"
                  value={productCodeFilter || null}
                  onChange={(v) => {
                    setProductCodeFilter(v);
                    setPage(1);
                  }}
                  placeholder="All products"
                  allowClear
                />
              </Field>
            </div>
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
        <DataTable<PricingRule>
          ariaLabel="Sales pricing rules"
          columns={pricingRuleColumns}
          data={loading || error ? [] : rules}
          loading={loading}
          error={error ? { message: error, onRetry: fetchRules } : null}
          getRowId={(rule) => rule.rule_id}
          pagination={
            totalPages > 1
              ? { page, totalPages, onPageChange: setPage }
              : undefined
          }
          emptyState={
            <span className="text-text-muted">No pricing rules found.</span>
          }
        />
      </div>

      <Drawer
        open={priceCheckOpen}
        onClose={() => setPriceCheckOpen(false)}
        title="Resolve price"
        width={560}
      >
        <ResolvePricePanel />
      </Drawer>

      {adding && (
        <PricingRuleDialog
          onClose={() => setAdding(false)}
          onSaved={() => setReload((n) => n + 1)}
        />
      )}
    </div>
  );
}

// ─── Add rule dialog ─────────────────────────────────────────────────────────

type TierRow = {
  id: number;
  min_gallons: number | null;
  max_gallons: number | null;
  /** Dollars per gallon. */
  price: number | null;
};

type RuleValues = {
  customer_id: string;
  product_code: string;
  strategy: PricingStrategy;
  priority: number | null;
  effective_date: string;
  expiry_date: string;
  /** Dollars (stored as cents). */
  posted_price: number | null;
  margin: number | null;
  freight_per_mile: number | null;
  tiers: TierRow[];
};

const toCents = (dollars: number | null) =>
  dollars == null ? null : Math.round(dollars * 100);

export function validatePricingRule(v: RuleValues) {
  const errors: Record<string, string | undefined> = {};
  if (!v.product_code) errors.product_code = "Pick a product.";
  if (v.priority == null || v.priority < 1)
    errors.priority = "Enter a priority of 1 or more.";
  if (!v.effective_date) errors.effective_date = "Enter the effective date.";
  if (v.expiry_date && v.effective_date && v.expiry_date <= v.effective_date)
    errors.expiry_date = "Expiry must be after the effective date.";
  if (v.strategy === "posted_price" && v.posted_price == null)
    errors.posted_price = "Enter the posted price.";
  if (
    (v.strategy === "rack_plus_margin" || v.strategy === "cost_plus") &&
    v.margin == null
  )
    errors.margin = "Enter the margin.";
  if (v.strategy === "cost_plus" && v.freight_per_mile == null)
    errors.freight_per_mile = "Enter the freight rate.";
  if (v.strategy === "tiered_volume") {
    if (v.tiers.length === 0) errors.tiers = "Add at least one tier.";
    for (let i = 0; i < v.tiers.length; i++) {
      const t = v.tiers[i];
      const last = i === v.tiers.length - 1;
      if (t.min_gallons == null || t.price == null) {
        errors.tiers = `Tier ${i + 1} needs a minimum and a price.`;
        break;
      }
      if (t.max_gallons == null && !last) {
        errors.tiers = `Only the last tier can be open-ended (tier ${i + 1}).`;
        break;
      }
      if (t.max_gallons != null && t.max_gallons < t.min_gallons) {
        errors.tiers = `Tier ${i + 1}: maximum is below the minimum.`;
        break;
      }
      const prev = v.tiers[i - 1];
      if (prev?.max_gallons != null && t.min_gallons <= prev.max_gallons) {
        errors.tiers = `Tier ${i + 1} overlaps tier ${i}.`;
        break;
      }
    }
  }
  return errors;
}

function PricingRuleDialog({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const submit = async (v: RuleValues) => {
    const data: CreatePricingRulePayload = {
      customer_id: v.customer_id || null,
      product_code: v.product_code,
      strategy: v.strategy,
      priority: v.priority ?? 10,
      effective_date: v.effective_date,
      expiry_date: v.expiry_date || null,
    };
    switch (v.strategy) {
      case "posted_price":
        data.posted_price_cents = toCents(v.posted_price);
        break;
      case "rack_plus_margin":
        data.margin_cents = toCents(v.margin);
        break;
      case "tiered_volume":
        data.tier_thresholds = v.tiers.map(
          (t): TierBreak => ({
            min_gallons: t.min_gallons ?? 0,
            max_gallons: t.max_gallons,
            price_cents: toCents(t.price) ?? 0,
          }),
        );
        break;
      case "cost_plus":
        data.margin_cents = toCents(v.margin);
        data.freight_rate_cents_per_mile = toCents(v.freight_per_mile);
        break;
    }
    await createPricingRule(data);
  };

  return (
    <FormDialog<RuleValues, void>
      open
      size="lg"
      title="Add pricing rule"
      submitLabel="Add Rule"
      successMessage="Pricing rule added"
      sections={[
        { id: "scope", title: "Scope" },
        { id: "price", title: "Price" },
      ]}
      initialValues={{
        customer_id: "",
        product_code: "",
        strategy: "posted_price",
        priority: 10,
        effective_date: "",
        expiry_date: "",
        posted_price: null,
        margin: null,
        freight_per_mile: null,
        tiers: [{ id: 0, min_gallons: 0, max_gallons: 1000, price: 3.5 }],
      }}
      validate={validatePricingRule}
      onSubmit={submit}
      onSaved={onSaved}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <FormSection id="scope" title="Scope">
            <Field
              label="Customer"
              span={1}
              help="Leave blank for the product default"
            >
              <CustomerPicker
                id="rule-customer-id"
                aria-label="Customer ID (optional)"
                value={values.customer_id || null}
                onChange={(v) => set("customer_id", v)}
                allowClear
                placeholder="Product default"
              />
            </Field>
            <Field
              label="Product"
              required
              span={1}
              error={errors.product_code}
            >
              <ProductPicker
                id="rule-product-code"
                aria-label="Product Code"
                value={values.product_code || null}
                onChange={(v) => set("product_code", v)}
                allowClear
              />
            </Field>
            <Field
              label="Priority"
              required
              span={1}
              help="Lower runs first"
              error={errors.priority}
            >
              <NumberField
                id="rule-priority"
                value={values.priority}
                onChange={(n) => set("priority", n)}
                decimals={0}
                min={1}
              />
            </Field>
            <Field label="Strategy" span={1}>
              <Select
                id="rule-strategy"
                value={values.strategy}
                onChange={(v) => set("strategy", v as PricingStrategy)}
                options={STRATEGIES}
              />
            </Field>
            <Field
              label="Effective date"
              required
              span={1}
              error={errors.effective_date}
            >
              <input
                id="rule-effective-date"
                type="date"
                value={values.effective_date}
                onChange={(e) => set("effective_date", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
            <Field
              label="Expiry date"
              span={1}
              help="Optional"
              error={errors.expiry_date}
            >
              <input
                id="rule-expiry-date"
                type="date"
                value={values.expiry_date}
                onChange={(e) => set("expiry_date", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
          </FormSection>
          <FormSection
            id="price"
            title={`Price · ${strategyLabel(values.strategy)}`}
          >
            {values.strategy === "posted_price" && (
              <Field
                label="Posted price"
                required
                span={1}
                help="Per gallon"
                error={errors.posted_price}
              >
                <NumberField
                  id="rule-posted-price"
                  value={values.posted_price}
                  onChange={(n) => set("posted_price", n)}
                  unit="$"
                  decimals={2}
                  min={0}
                />
              </Field>
            )}
            {(values.strategy === "rack_plus_margin" ||
              values.strategy === "cost_plus") && (
              <Field
                label="Margin"
                required
                span={1}
                help={
                  values.strategy === "rack_plus_margin"
                    ? "Per gallon above rack"
                    : "Per gallon"
                }
                error={errors.margin}
              >
                <NumberField
                  id="rule-margin"
                  value={values.margin}
                  onChange={(n) => set("margin", n)}
                  unit="$"
                  decimals={2}
                  min={0}
                />
              </Field>
            )}
            {values.strategy === "cost_plus" && (
              <Field
                label="Freight rate"
                required
                span={1}
                help="Per mile"
                error={errors.freight_per_mile}
              >
                <NumberField
                  id="rule-freight-rate"
                  value={values.freight_per_mile}
                  onChange={(n) => set("freight_per_mile", n)}
                  unit="$"
                  decimals={2}
                  min={0}
                />
              </Field>
            )}
            {values.strategy === "tiered_volume" && (
              <TierRows
                tiers={values.tiers}
                onChange={(tiers) => set("tiers", tiers)}
                error={errors.tiers}
              />
            )}
          </FormSection>
        </>
      )}
    </FormDialog>
  );
}

function TierRows({
  tiers,
  onChange,
  error,
}: {
  tiers: TierRow[];
  onChange: (tiers: TierRow[]) => void;
  error?: string;
}) {
  const update = (id: number, patch: Partial<TierRow>) =>
    onChange(tiers.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  return (
    <div className="col-span-2">
      <p className="mb-2 text-xs text-text-muted">
        Leave the last tier's maximum empty for no upper limit.
      </p>
      <table className="w-full text-sm">
        <caption className="sr-only">Volume tiers</caption>
        <thead>
          <tr className="text-left text-xs text-text-muted">
            <th scope="col" className="pb-1 font-medium">
              From
            </th>
            <th scope="col" className="pb-1 font-medium">
              To
            </th>
            <th scope="col" className="pb-1 font-medium">
              Price per gallon
            </th>
            <th scope="col">
              <span className="sr-only">Remove</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {tiers.map((t, i) => (
            <tr key={t.id}>
              <td className="py-1 pr-2">
                <NumberField
                  aria-label={`Tier ${i + 1} minimum gallons`}
                  value={t.min_gallons}
                  onChange={(n) => update(t.id, { min_gallons: n })}
                  unit="gal"
                  decimals={0}
                  min={0}
                />
              </td>
              <td className="py-1 pr-2">
                <NumberField
                  aria-label={`Tier ${i + 1} maximum gallons`}
                  value={t.max_gallons}
                  onChange={(n) => update(t.id, { max_gallons: n })}
                  unit="gal"
                  decimals={0}
                  min={0}
                  placeholder="No limit"
                />
              </td>
              <td className="py-1 pr-2">
                <NumberField
                  aria-label={`Tier ${i + 1} price`}
                  value={t.price}
                  onChange={(n) => update(t.id, { price: n })}
                  unit="$"
                  decimals={2}
                  min={0}
                />
              </td>
              <td className="py-1">
                {tiers.length > 1 && (
                  <IconButton
                    label={`Remove tier ${i + 1}`}
                    size="sm"
                    icon={<Trash2 className="h-3.5 w-3.5" />}
                    onClick={() => onChange(tiers.filter((x) => x.id !== t.id))}
                  />
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && (
        <p role="alert" className="mt-1 text-xs text-red-800">
          {error}
        </p>
      )}
      <Button
        type="button"
        size="sm"
        variant="secondary"
        className="mt-2"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => {
          const last = tiers[tiers.length - 1];
          onChange([
            ...tiers,
            {
              id: Date.now(),
              min_gallons: last?.max_gallons != null ? last.max_gallons + 1 : 0,
              max_gallons: null,
              price: null,
            },
          ]);
        }}
      >
        Add tier
      </Button>
    </div>
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
        gallons: parseNumber(gallons) ?? 0,
      };
      if (terminalId) payload.terminal_id = terminalId;
      if (routeMiles)
        payload.route_miles = parseNumber(routeMiles) ?? undefined;

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
    <div className="p-4">
      <p className="text-sm text-text-muted mb-4">
        Test the pricing engine by entering customer, product, and volume to see
        the resolved price.
      </p>

      <form
        onSubmit={handleResolve}
        className="rounded-lg border border-slate-200 p-4"
      >
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
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
              type="text"
              inputMode="decimal"
              value={gallons}
              onChange={(e) => setGallons(e.target.value)}
              className={INPUT_CLASS}
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
              className={INPUT_CLASS}
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
              type="text"
              inputMode="decimal"
              value={routeMiles}
              onChange={(e) => setRouteMiles(e.target.value)}
              className={INPUT_CLASS}
              placeholder="25"
            />
          </div>
          <div className="flex items-end">
            <Button type="submit" loading={resolving} fullWidth>
              Resolve Price
            </Button>
          </div>
        </div>
      </form>

      {/* Resolve result */}
      {result && (
        <div className="mt-4 rounded-lg border border-slate-200 p-4">
          <h3 className="mb-2 text-sm font-semibold text-text">
            Price Resolved
          </h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <span className="text-gray-500 block">Resolved Price</span>
              <span className="font-bold text-lg">
                {formatCents(result.resolved_price_cents)}
              </span>
              <span className="text-gray-500 text-xs">/gal</span>
            </div>
            <div>
              <span className="text-gray-500 block">Strategy Used</span>
              <span className="font-medium">
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
                    {humanize(key)}: {formatCents(value)}
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
          className="mt-4 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-800"
        >
          {resolveError}
        </div>
      )}
    </div>
  );
}
