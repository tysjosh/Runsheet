"use client";

/**
 * Billing → Price books (UI revamp task 3.4, design.md §5 "Price book +
 * rules": lg FormDialog with sections Book and Rules, plus an sm add-rule
 * sub-dialog). The list is a DataTable; "New price book" sits in the hub's
 * title row; Activate is a row action. The price-check calculator (a dry run,
 * not create/edit) stays inline, in a drawer opened from the toolbar.
 */
import { Calculator, CheckCircle2, Pencil, Plus, Trash2 } from "lucide-react";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Button,
  type Column,
  DataTable,
  Drawer,
  Field,
  FormDialog,
  FormSection,
  IconButton,
  INPUT_CLASS,
  InlineBanner,
  NumberField,
  ProductChip,
  ProductSelect,
  Select,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { calendarDate, gallons, money, number } from "../../lib/format";
import type {
  PriceBook,
  PriceBookStatus,
  PricingResolveRequest,
  PricingResolveResult,
  PricingRule,
  PricingScopeType,
} from "../../services/commerceApi";
import {
  activatePriceBook,
  createPriceBook,
  getPriceBook,
  getPriceBooks,
  resolvePricing,
  updatePriceBook,
} from "../../services/commerceApi";
import type { StatusKey } from "../../styles/tokens";
import { PageTitle } from "../ui/PageHeader";
import { notify } from "../ui/toast/notify";

interface PriceBookEditorProps {
  /** Opens this book's editor on load. */
  priceBookId?: string;
}

const BOOK_STATUS: Record<
  PriceBookStatus,
  { status: StatusKey; label: string }
> = {
  draft: { status: "draft", label: "Draft" },
  active: { status: "ok", label: "Active" },
  archived: { status: "cancelled", label: "Archived" },
};

const SCOPES: { value: PricingScopeType; label: string }[] = [
  { value: "default", label: "Default (everyone)" },
  { value: "tier", label: "Account tier" },
  { value: "account", label: "One account" },
];

type DraftRule = Omit<PricingRule, "price_book_id" | "created_at">;

type BookValues = { name: string; description: string; rules: DraftRule[] };

type RuleValues = {
  product_code: string;
  scope_type: PricingScopeType;
  scope_value: string;
  /** Dollars per gallon (stored as integer cents). */
  unit_price: number | null;
  min_quantity_gallons: number | null;
  effective_from: string;
  effective_to: string;
};

const today = () => new Date().toISOString().split("T")[0];

function scopeLabel(rule: Pick<DraftRule, "scope_type" | "scope_value">) {
  if (rule.scope_type === "default") return "Default";
  const kind = rule.scope_type === "tier" ? "Tier" : "Account";
  return rule.scope_value ? `${kind}: ${rule.scope_value}` : kind;
}

export function validateRule(v: RuleValues) {
  const errors: Record<string, string | undefined> = {};
  if (!v.product_code) errors.product_code = "Pick a product.";
  if (v.scope_type !== "default" && !v.scope_value.trim())
    errors.scope_value =
      v.scope_type === "tier" ? "Enter the tier." : "Enter the account ID.";
  if (v.unit_price == null) errors.unit_price = "Enter a price.";
  else if (v.unit_price < 0) errors.unit_price = "Price can't be negative.";
  if (v.min_quantity_gallons != null && v.min_quantity_gallons < 0)
    errors.min_quantity_gallons = "Minimum can't be negative.";
  if (!v.effective_from) errors.effective_from = "Pick a start date.";
  if (v.effective_to && v.effective_from && v.effective_to < v.effective_from)
    errors.effective_to = "End date is before the start date.";
  return errors;
}

export default function PriceBookEditor({ priceBookId }: PriceBookEditorProps) {
  const [priceBooks, setPriceBooks] = useState<PriceBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  // The book dialog: null = closed; "new" = create; otherwise the loaded book.
  const [editing, setEditing] = useState<
    null | "new" | { book: PriceBook; rules: DraftRule[] }
  >(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [priceCheckOpen, setPriceCheckOpen] = useState(false);
  // Rule sub-dialog: index into the book dialog's rules, or "new".
  const [ruleTarget, setRuleTarget] = useState<null | "new" | number>(null);
  const bookForm = useRef<{
    values: BookValues;
    setValues: (next: (prev: BookValues) => BookValues) => void;
  } | null>(null);

  const fetchPriceBooks = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getPriceBooks();
      setPriceBooks(res.data ?? []);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load price books",
      );
    } finally {
      setLoading(false);
    }
    // `reload` refetches after a save.
  }, [reload]);

  useEffect(() => {
    fetchPriceBooks();
  }, [fetchPriceBooks]);

  const openBook = useCallback(async (bookId: string) => {
    setOpening(bookId);
    setActionError(null);
    try {
      const res = await getPriceBook(bookId);
      const { rules, ...book } = res.data;
      setEditing({
        book,
        rules: (rules ?? []).map(
          ({ price_book_id: _p, created_at: _c, ...r }) => r,
        ),
      });
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to load price book",
      );
    } finally {
      setOpening(null);
    }
  }, []);

  useEffect(() => {
    if (priceBookId) void openBook(priceBookId);
  }, [priceBookId, openBook]);

  const activate = async (book: PriceBook) => {
    setActionError(null);
    try {
      await activatePriceBook(book.price_book_id);
      notify({ type: "success", message: `${book.name} is now active` });
      setReload((n) => n + 1);
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to activate price book",
      );
    }
  };

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setEditing("new")}
      >
        New price book
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  const columns: Column<PriceBook>[] = [
    {
      key: "name",
      header: "Name",
      truncate: true,
      title: (b) => b.name,
      className: "font-medium text-text",
      cell: (b) => b.name,
    },
    {
      key: "status",
      header: "Status",
      width: 120,
      cell: (b) => {
        const s = BOOK_STATUS[b.status] ?? BOOK_STATUS.draft;
        return <StatusBadge status={s.status} label={s.label} />;
      },
    },
    {
      key: "rule_count",
      header: "Rules",
      align: "right",
      width: 90,
      className: "tabular-nums text-slate-700",
      cell: (b) => number(b.rule_count),
    },
    {
      key: "description",
      header: "Description",
      truncate: true,
      title: (b) => b.description ?? undefined,
      className: "text-slate-700",
      cell: (b) => b.description || "—",
    },
    {
      key: "updated_at",
      header: "Updated",
      width: 150,
      className: "text-slate-700",
      cell: (b) => calendarDate(b.updated_at),
    },
  ];

  const saveBook = async (v: BookValues): Promise<PriceBook> => {
    const payload = {
      name: v.name.trim(),
      description: v.description.trim() || undefined,
      rules: v.rules.map(({ rule_id: _id, ...rest }) => rest),
    };
    if (editing === "new") return (await createPriceBook(payload)).data;
    if (!editing) throw new Error("No price book open");
    return (await updatePriceBook(editing.book.price_book_id, payload)).data;
  };

  const ruleInitial = (): RuleValues => {
    const rule =
      typeof ruleTarget === "number"
        ? bookForm.current?.values.rules[ruleTarget]
        : undefined;
    return rule
      ? {
          product_code: rule.product_code,
          scope_type: rule.scope_type,
          scope_value: rule.scope_value ?? "",
          unit_price: rule.unit_price_cents / 100,
          min_quantity_gallons: rule.min_quantity_gallons,
          effective_from: rule.effective_from,
          effective_to: rule.effective_to ?? "",
        }
      : {
          product_code: "",
          scope_type: "default",
          scope_value: "",
          unit_price: null,
          min_quantity_gallons: null,
          effective_from: today(),
          effective_to: "",
        };
  };

  const saveRule = (v: RuleValues) => {
    const target = ruleTarget;
    const form = bookForm.current;
    if (!form || target === null) return;
    form.setValues((prev) => {
      const existing = typeof target === "number" ? prev.rules[target] : null;
      const rule: DraftRule = {
        rule_id: existing?.rule_id ?? `new-${Date.now()}`,
        product_code: v.product_code,
        scope_type: v.scope_type,
        scope_value: v.scope_type === "default" ? "" : v.scope_value.trim(),
        unit_price_cents: Math.round((v.unit_price ?? 0) * 100),
        min_quantity_gallons: v.min_quantity_gallons,
        effective_from: v.effective_from,
        effective_to: v.effective_to || null,
      };
      const rules =
        typeof target === "number"
          ? prev.rules.map((r, i) => (i === target ? rule : r))
          : [...prev.rules, rule];
      return { ...prev, rules };
    });
  };

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Price books
          </PageTitle>
          <div className="ml-auto">{actions}</div>
        </div>
      )}
      <Toolbar
        label="Price books"
        filters={
          actionError ? (
            <span role="alert" className="truncate text-xs text-red-800">
              {actionError}
            </span>
          ) : undefined
        }
        end={
          <Button
            size="sm"
            variant="ghost"
            icon={<Calculator className="h-3.5 w-3.5" />}
            onClick={() => setPriceCheckOpen(true)}
          >
            Price check
          </Button>
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<PriceBook>
          ariaLabel="Price books"
          columns={columns}
          data={loading || error ? [] : priceBooks}
          loading={loading}
          error={error ? { message: error, onRetry: fetchPriceBooks } : null}
          getRowId={(b) => b.price_book_id}
          rowLabel={(b) => b.name}
          onRowClick={(b) => void openBook(b.price_book_id)}
          rowMenu={(b) => [
            {
              id: "edit",
              label: opening === b.price_book_id ? "Opening…" : "Edit",
              icon: <Pencil className="h-3.5 w-3.5" />,
              onSelect: () => void openBook(b.price_book_id),
            },
            ...(b.status === "draft"
              ? [
                  {
                    id: "activate",
                    label: "Activate",
                    icon: <CheckCircle2 className="h-3.5 w-3.5" />,
                    onSelect: () => void activate(b),
                  },
                ]
              : []),
          ]}
          emptyState={
            <div className="text-text-muted">
              <p className="text-sm font-medium">No price books yet</p>
              <p className="mt-1 text-xs">
                Create a price book to start setting prices.
              </p>
            </div>
          }
        />
      </div>

      {editing && (
        <FormDialog<BookValues, PriceBook>
          open
          size="lg"
          title={
            editing === "new" ? "New price book" : `Edit ${editing.book.name}`
          }
          submitLabel={editing === "new" ? "Create price book" : "Save changes"}
          successMessage={
            editing === "new" ? "Price book created" : "Price book saved"
          }
          sections={[
            { id: "book", title: "Book" },
            { id: "rules", title: "Rules" },
          ]}
          initialValues={
            editing === "new"
              ? { name: "", description: "", rules: [] }
              : {
                  name: editing.book.name,
                  description: editing.book.description ?? "",
                  rules: editing.rules,
                }
          }
          validate={(v) => ({
            name: v.name.trim() ? undefined : "Enter a name.",
          })}
          onSubmit={saveBook}
          onSaved={() => setReload((n) => n + 1)}
          onClose={() => {
            setEditing(null);
            setRuleTarget(null);
          }}
        >
          {({ values, set, setValues, errors }) => {
            bookForm.current = { values, setValues };
            return (
              <>
                <FormSection id="book" title="Book">
                  <Field label="Name" required error={errors.name}>
                    <input
                      id="price-book-name"
                      type="text"
                      value={values.name}
                      onChange={(e) => set("name", e.target.value)}
                      placeholder="e.g. 2026 Q4 commercial diesel"
                      className={INPUT_CLASS}
                    />
                  </Field>
                  <Field label="Description">
                    <textarea
                      id="price-book-description"
                      value={values.description}
                      onChange={(e) => set("description", e.target.value)}
                      rows={2}
                      placeholder="Optional"
                      className={`${INPUT_CLASS} h-auto py-1.5`}
                    />
                  </Field>
                </FormSection>
                <FormSection
                  id="rules"
                  title={`Rules (${number(values.rules.length)})`}
                >
                  <div className="col-span-2">
                    {values.rules.length === 0 ? (
                      <p className="text-sm text-text-muted">
                        No rules yet. Add one to set a price.
                      </p>
                    ) : (
                      <ul
                        aria-label="Pricing rules"
                        className="divide-y divide-slate-100 rounded-lg border border-slate-200"
                      >
                        {values.rules.map((r, i) => (
                          <li
                            key={r.rule_id}
                            className="flex items-center gap-3 px-3 py-2 text-sm"
                          >
                            <ProductChip code={r.product_code} />
                            <span className="min-w-0 flex-1 truncate text-slate-700">
                              {scopeLabel(r)}
                              {r.min_quantity_gallons != null &&
                                ` · from ${gallons(r.min_quantity_gallons)}`}
                              {" · "}
                              {calendarDate(r.effective_from)}
                              {r.effective_to
                                ? ` – ${calendarDate(r.effective_to)}`
                                : " onward"}
                            </span>
                            <span className="font-semibold tabular-nums text-text">
                              {money(r.unit_price_cents / 100)}/gal
                            </span>
                            <IconButton
                              label={`Edit rule ${i + 1}`}
                              size="sm"
                              icon={<Pencil className="h-3.5 w-3.5" />}
                              onClick={() => setRuleTarget(i)}
                            />
                            <IconButton
                              label={`Delete rule ${i + 1}`}
                              size="sm"
                              icon={<Trash2 className="h-3.5 w-3.5" />}
                              onClick={() =>
                                set(
                                  "rules",
                                  values.rules.filter((_, j) => j !== i),
                                )
                              }
                            />
                          </li>
                        ))}
                      </ul>
                    )}
                    <Button
                      type="button"
                      className="mt-2"
                      size="sm"
                      variant="secondary"
                      icon={<Plus className="h-3.5 w-3.5" />}
                      onClick={() => setRuleTarget("new")}
                    >
                      Add rule
                    </Button>
                  </div>
                </FormSection>
              </>
            );
          }}
        </FormDialog>
      )}

      {editing && ruleTarget !== null && (
        <FormDialog<RuleValues, void>
          open
          size="sm"
          title={ruleTarget === "new" ? "Add rule" : "Edit rule"}
          help="Saved with the price book."
          submitLabel={ruleTarget === "new" ? "Add rule" : "Update rule"}
          successMessage={null}
          initialValues={ruleInitial()}
          validate={validateRule}
          onSubmit={saveRule}
          onClose={() => setRuleTarget(null)}
        >
          {({ values, set, errors }) => (
            <>
              <Field label="Product" required error={errors.product_code}>
                <ProductSelect
                  id="rule-product"
                  value={values.product_code || null}
                  onChange={(code) => set("product_code", code)}
                />
              </Field>
              <Field label="Applies to" span={1}>
                <Select
                  id="rule-scope-type"
                  value={values.scope_type}
                  onChange={(v) => set("scope_type", v as PricingScopeType)}
                  options={SCOPES}
                />
              </Field>
              <Field
                label={values.scope_type === "tier" ? "Tier" : "Account ID"}
                span={1}
                error={errors.scope_value}
              >
                <input
                  id="rule-scope-value"
                  type="text"
                  value={values.scope_value}
                  disabled={values.scope_type === "default"}
                  onChange={(e) => set("scope_value", e.target.value)}
                  placeholder={
                    values.scope_type === "default"
                      ? "Not needed"
                      : values.scope_type === "tier"
                        ? "e.g. gold"
                        : "acc_…"
                  }
                  className={INPUT_CLASS}
                />
              </Field>
              <Field
                label="Unit price"
                required
                span={1}
                error={errors.unit_price}
              >
                <NumberField
                  id="rule-price"
                  value={values.unit_price}
                  onChange={(n) => set("unit_price", n)}
                  unit="$"
                  decimals={2}
                  min={0}
                />
              </Field>
              <Field
                label="Minimum quantity"
                span={1}
                help="Optional"
                error={errors.min_quantity_gallons}
              >
                <NumberField
                  id="rule-min-qty"
                  value={values.min_quantity_gallons}
                  onChange={(n) => set("min_quantity_gallons", n)}
                  unit="gal"
                  decimals={0}
                  min={0}
                />
              </Field>
              <Field
                label="Effective from"
                required
                span={1}
                error={errors.effective_from}
              >
                <input
                  id="rule-effective-from"
                  type="date"
                  value={values.effective_from}
                  onChange={(e) => set("effective_from", e.target.value)}
                  className={INPUT_CLASS}
                />
              </Field>
              <Field label="Until" span={1} error={errors.effective_to}>
                <input
                  id="rule-effective-to"
                  type="date"
                  value={values.effective_to}
                  onChange={(e) => set("effective_to", e.target.value)}
                  className={INPUT_CLASS}
                />
              </Field>
            </>
          )}
        </FormDialog>
      )}

      <Drawer
        open={priceCheckOpen}
        onClose={() => setPriceCheckOpen(false)}
        title="Price check"
        width={480}
      >
        <PriceCheck />
      </Drawer>
    </div>
  );
}

/** Dry-run price resolution (a calculator, not create/edit: stays inline). */
function PriceCheck() {
  const [request, setRequest] = useState<PricingResolveRequest>({
    account_id: "",
    product_code: "",
    quantity_gallons: 100,
  });
  const [result, setResult] = useState<PricingResolveResult | null>(null);
  const [resolving, setResolving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const resolve = async (e: FormEvent) => {
    e.preventDefault();
    if (!request.account_id || !request.product_code) {
      setError("Enter an account and pick a product.");
      return;
    }
    setResolving(true);
    setError(null);
    setResult(null);
    try {
      const res = await resolvePricing(request);
      setResult(res.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Pricing resolution failed",
      );
    } finally {
      setResolving(false);
    }
  };

  return (
    <form onSubmit={resolve} className="space-y-3 p-4">
      <p className="text-sm text-text-muted">
        See which rule prices an order, without saving anything.
      </p>
      <Field label="Account ID">
        <input
          id="resolve-account"
          type="text"
          value={request.account_id}
          onChange={(e) =>
            setRequest({ ...request, account_id: e.target.value })
          }
          placeholder="acc_…"
          className={INPUT_CLASS}
        />
      </Field>
      <Field label="Product">
        <ProductSelect
          id="resolve-product"
          value={request.product_code || null}
          onChange={(code) => setRequest({ ...request, product_code: code })}
        />
      </Field>
      <Field label="Quantity">
        <NumberField
          id="resolve-quantity"
          value={request.quantity_gallons}
          onChange={(n) => setRequest({ ...request, quantity_gallons: n ?? 1 })}
          unit="gal"
          decimals={0}
          min={1}
        />
      </Field>
      <Button type="submit" variant="secondary" loading={resolving}>
        Check price
      </Button>
      {error && (
        <div role="alert">
          <InlineBanner tone="critical">{error}</InlineBanner>
        </div>
      )}
      {result && (
        <dl className="grid grid-cols-2 gap-3 rounded-lg border border-slate-200 p-3 text-sm">
          <div>
            <dt className="text-xs text-text-muted">Unit price</dt>
            <dd className="font-semibold tabular-nums">
              {money(result.unit_price_cents / 100)}/gal
            </dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Applies to</dt>
            <dd>
              {scopeLabel({ scope_type: result.scope_type, scope_value: "" })}
            </dd>
          </div>
          <div className="col-span-2">
            <dt className="text-xs text-text-muted">Matched rule</dt>
            <dd className="font-mono text-xs">{result.rule_id}</dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Cache</dt>
            <dd>{result.matched_from_cache ? "Hit" : "Miss"}</dd>
          </div>
        </dl>
      )}
    </form>
  );
}
