"use client";

import type React from "react";
import { useCallback, useEffect, useState } from "react";
import { Badge, Button, EmptyState, PageHeader, Table } from "@/components/ui";
import type {
  PriceBook,
  PricingResolveRequest,
  PricingResolveResult,
  PricingRule,
} from "../../services/commerceApi";
import {
  activatePriceBook,
  createPriceBook,
  getPriceBook,
  getPriceBooks,
  resolvePricing,
  updatePriceBook,
} from "../../services/commerceApi";

interface PriceBookEditorProps {
  priceBookId?: string;
  onBack?: () => void;
}

interface RuleFormState {
  product_code: string;
  scope_type: "account" | "tier" | "default";
  scope_value: string;
  unit_price_cents: number;
  min_quantity_gallons: number | null;
  effective_from: string;
  effective_to: string;
}

const emptyRule: RuleFormState = {
  product_code: "",
  scope_type: "default",
  scope_value: "",
  unit_price_cents: 0,
  min_quantity_gallons: null,
  effective_from: new Date().toISOString().split("T")[0],
  effective_to: "",
};

interface SelectedBookState {
  price_book_id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  status: "draft" | "active" | "archived";
  rule_count: number;
  created_at: string;
  updated_at: string;
}

export default function PriceBookEditor({
  priceBookId,
  onBack,
}: PriceBookEditorProps) {
  const [priceBooks, setPriceBooks] = useState<PriceBook[]>([]);
  const [selectedBook, setSelectedBook] = useState<SelectedBookState | null>(
    null,
  );
  const [rules, setRules] = useState<PricingRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [editingRule, setEditingRule] = useState<RuleFormState | null>(null);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  // Resolve preview state
  const [resolveRequest, setResolveRequest] = useState<PricingResolveRequest>({
    account_id: "",
    product_code: "",
    quantity_gallons: 100,
  });
  const [resolveResult, setResolveResult] =
    useState<PricingResolveResult | null>(null);
  const [resolving, setResolving] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);

  // Create price book modal state
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const fetchPriceBooks = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getPriceBooks();
      setPriceBooks(res.data);
      if (priceBookId) {
        const bookRes = await getPriceBook(priceBookId);
        const { rules: bookRules, ...bookData } = bookRes.data;
        setSelectedBook(bookData);
        setRules(bookRules || []);
      }
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load price books",
      );
    } finally {
      setLoading(false);
    }
  }, [priceBookId]);

  useEffect(() => {
    fetchPriceBooks();
  }, [fetchPriceBooks]);

  const handleSelectBook = async (bookId: string) => {
    setLoading(true);
    try {
      const res = await getPriceBook(bookId);
      const { rules: bookRules, ...bookData } = res.data;
      setSelectedBook(bookData);
      setRules(bookRules || []);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load price book",
      );
    } finally {
      setLoading(false);
    }
  };

  const handleAddRule = () => {
    setEditingRule({ ...emptyRule });
    setEditingIndex(null);
  };

  const handleEditRule = (index: number) => {
    const rule = rules[index];
    setEditingRule({
      product_code: rule.product_code,
      scope_type: rule.scope_type,
      scope_value: rule.scope_value || "",
      unit_price_cents: rule.unit_price_cents,
      min_quantity_gallons: rule.min_quantity_gallons,
      effective_from: rule.effective_from,
      effective_to: rule.effective_to || "",
    });
    setEditingIndex(index);
  };

  const handleDeleteRule = (index: number) => {
    setRules((prev) => prev.filter((_, i) => i !== index));
  };

  const handleSaveRule = () => {
    if (!editingRule) return;
    const newRule: PricingRule = {
      rule_id:
        editingIndex !== null
          ? rules[editingIndex].rule_id
          : `new-${Date.now()}`,
      price_book_id: selectedBook?.price_book_id || "",
      product_code: editingRule.product_code,
      scope_type: editingRule.scope_type,
      scope_value: editingRule.scope_value || "",
      unit_price_cents: editingRule.unit_price_cents,
      min_quantity_gallons: editingRule.min_quantity_gallons,
      effective_from: editingRule.effective_from,
      effective_to: editingRule.effective_to || null,
      created_at: new Date().toISOString(),
    };

    if (editingIndex !== null) {
      setRules((prev) =>
        prev.map((r, i) => (i === editingIndex ? newRule : r)),
      );
    } else {
      setRules((prev) => [...prev, newRule]);
    }
    setEditingRule(null);
    setEditingIndex(null);
  };

  const handleSaveBook = async () => {
    if (!selectedBook) return;
    setSaving(true);
    setError(null);
    try {
      await updatePriceBook(selectedBook.price_book_id, {
        name: selectedBook.name,
        description: selectedBook.description || undefined,
        rules: rules.map(
          ({ rule_id, price_book_id, created_at, ...rest }) => rest,
        ),
      });
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to save price book",
      );
    } finally {
      setSaving(false);
    }
  };

  const openCreateModal = () => {
    setCreateName("");
    setCreateDescription("");
    setCreateError(null);
    setShowCreateModal(true);
  };

  const handleCreateBook = async (e: React.FormEvent) => {
    e.preventDefault();
    const name = createName.trim();
    if (!name) {
      setCreateError("Name is required");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const res = await createPriceBook({
        name,
        description: createDescription.trim() || undefined,
        rules: [],
      });
      setShowCreateModal(false);
      // Refresh the list and open the newly created book for editing.
      await fetchPriceBooks();
      await handleSelectBook(res.data.price_book_id);
    } catch (err) {
      setCreateError(
        err instanceof Error ? err.message : "Failed to create price book",
      );
    } finally {
      setCreating(false);
    }
  };

  const handleActivate = async () => {
    if (!selectedBook) return;
    setSaving(true);
    setError(null);
    try {
      const res = await activatePriceBook(selectedBook.price_book_id);
      setSelectedBook(res.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to activate price book",
      );
    } finally {
      setSaving(false);
    }
  };

  const handleResolve = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!resolveRequest.account_id || !resolveRequest.product_code) return;
    setResolving(true);
    setResolveError(null);
    setResolveResult(null);
    try {
      const res = await resolvePricing(resolveRequest);
      setResolveResult(res.data);
    } catch (err) {
      setResolveError(
        err instanceof Error ? err.message : "Pricing resolution failed",
      );
    } finally {
      setResolving(false);
    }
  };

  const formatCents = (cents: number) => `$${(cents / 100).toFixed(2)}`;

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12">
        <span className="sr-only">Loading price books...</span>
        <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
      </div>
    );
  }

  return (
    <div className="p-6">
      <PageHeader
        title="Price Book Editor"
        subtitle="Manage pricing rules and test resolution with the dry-run preview."
        actions={
          onBack ? (
            <Button variant="ghost" onClick={onBack}>
              ← Back
            </Button>
          ) : undefined
        }
      />

      {error && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* Price book selector */}
      {!selectedBook && (
        <section aria-labelledby="books-heading" className="mb-8">
          <div className="flex items-center justify-between mb-3">
            <h2 id="books-heading" className="text-lg font-semibold">
              Select a Price Book
            </h2>
            <Button variant="primary" size="sm" onClick={openCreateModal}>
              Create Price Book
            </Button>
          </div>
          {priceBooks.length === 0 ? (
            <EmptyState
              title="No price books found."
              description="Create a price book to get started with pricing rules."
              action={{
                label: "Create Price Book",
                onClick: openCreateModal,
              }}
            />
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {priceBooks.map((book) => (
                <button
                  key={book.price_book_id}
                  type="button"
                  onClick={() => handleSelectBook(book.price_book_id)}
                  className="border rounded p-4 text-left hover:bg-gray-50"
                >
                  <p className="font-medium">{book.name}</p>
                  <p className="text-sm text-gray-600">
                    {book.description || "No description"}
                  </p>
                  <Badge
                    variant={
                      book.status === "active"
                        ? "success"
                        : book.status === "draft"
                          ? "warning"
                          : "neutral"
                    }
                    size="sm"
                    className="mt-2"
                  >
                    {book.status}
                  </Badge>
                </button>
              ))}
            </div>
          )}
        </section>
      )}

      {/* Rule editor */}
      {selectedBook && (
        <>
          <section aria-labelledby="rules-heading" className="mb-8">
            <div className="flex items-center justify-between mb-3">
              <h2 id="rules-heading" className="text-lg font-semibold">
                {selectedBook.name} — Rules ({rules.length})
              </h2>
              <div className="flex gap-2">
                <Button variant="primary" size="sm" onClick={handleAddRule}>
                  Add Rule
                </Button>
                <Button
                  variant="success"
                  size="sm"
                  onClick={handleSaveBook}
                  disabled={saving}
                  loading={saving}
                >
                  Save
                </Button>
                {selectedBook.status === "draft" && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={handleActivate}
                    disabled={saving}
                  >
                    Activate
                  </Button>
                )}
              </div>
            </div>

            <Table
              columns={[
                { key: "product_code", label: "Product" },
                {
                  key: "scope",
                  label: "Scope",
                  render: (rule: PricingRule & { idx: number }) =>
                    `${rule.scope_type}${rule.scope_value ? `: ${rule.scope_value}` : ""}`,
                },
                {
                  key: "price",
                  label: "Price",
                  render: (rule: PricingRule & { idx: number }) =>
                    formatCents(rule.unit_price_cents),
                },
                {
                  key: "min_qty",
                  label: "Min Qty",
                  render: (rule: PricingRule & { idx: number }) =>
                    `${rule.min_quantity_gallons ?? "—"} gal+`,
                },
                {
                  key: "effective",
                  label: "Effective",
                  render: (rule: PricingRule & { idx: number }) =>
                    `${rule.effective_from}${rule.effective_to ? ` → ${rule.effective_to}` : ""}`,
                  className: "text-sm",
                },
                {
                  key: "actions",
                  label: "Actions",
                  render: (rule: PricingRule & { idx: number }) => (
                    <div className="flex gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleEditRule(rule.idx)}
                      >
                        Edit
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleDeleteRule(rule.idx)}
                        className="text-error hover:text-error-dark"
                      >
                        Delete
                      </Button>
                    </div>
                  ),
                },
              ]}
              data={rules.map((rule, idx) => ({ ...rule, idx }))}
              keyExtractor={(item) => item.rule_id}
              emptyState={
                <EmptyState
                  title="No rules defined"
                  description='Click "Add Rule" to create one.'
                />
              }
            />
          </section>

          {/* Rule edit form */}
          {editingRule && (
            <section
              aria-labelledby="rule-form-heading"
              className="mb-8 border rounded p-4 bg-gray-50"
            >
              <h3 id="rule-form-heading" className="font-semibold mb-3">
                {editingIndex !== null ? "Edit Rule" : "New Rule"}
              </h3>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div>
                  <label
                    htmlFor="rule-product"
                    className="block text-sm font-medium mb-1"
                  >
                    Product Code
                  </label>
                  <input
                    id="rule-product"
                    type="text"
                    value={editingRule.product_code}
                    onChange={(e) =>
                      setEditingRule({
                        ...editingRule,
                        product_code: e.target.value,
                      })
                    }
                    className="w-full border rounded px-3 py-2"
                  />
                </div>
                <div>
                  <label
                    htmlFor="rule-scope-type"
                    className="block text-sm font-medium mb-1"
                  >
                    Scope Type
                  </label>
                  <select
                    id="rule-scope-type"
                    value={editingRule.scope_type}
                    onChange={(e) =>
                      setEditingRule({
                        ...editingRule,
                        scope_type: e.target.value as
                          | "account"
                          | "tier"
                          | "default",
                      })
                    }
                    className="w-full border rounded px-3 py-2"
                  >
                    <option value="default">Default</option>
                    <option value="tier">Tier</option>
                    <option value="account">Account</option>
                  </select>
                </div>
                <div>
                  <label
                    htmlFor="rule-scope-id"
                    className="block text-sm font-medium mb-1"
                  >
                    Scope Value
                  </label>
                  <input
                    id="rule-scope-id"
                    type="text"
                    value={editingRule.scope_value}
                    onChange={(e) =>
                      setEditingRule({
                        ...editingRule,
                        scope_value: e.target.value,
                      })
                    }
                    className="w-full border rounded px-3 py-2"
                    placeholder="Account/tier ID (optional for default)"
                  />
                </div>
                <div>
                  <label
                    htmlFor="rule-price"
                    className="block text-sm font-medium mb-1"
                  >
                    Unit Price (cents)
                  </label>
                  <input
                    id="rule-price"
                    type="number"
                    min={0}
                    value={editingRule.unit_price_cents}
                    onChange={(e) =>
                      setEditingRule({
                        ...editingRule,
                        unit_price_cents: parseInt(e.target.value, 10) || 0,
                      })
                    }
                    className="w-full border rounded px-3 py-2"
                  />
                </div>
                <div>
                  <label
                    htmlFor="rule-effective-from"
                    className="block text-sm font-medium mb-1"
                  >
                    Effective From
                  </label>
                  <input
                    id="rule-effective-from"
                    type="date"
                    value={editingRule.effective_from}
                    onChange={(e) =>
                      setEditingRule({
                        ...editingRule,
                        effective_from: e.target.value,
                      })
                    }
                    className="w-full border rounded px-3 py-2"
                  />
                </div>
                <div>
                  <label
                    htmlFor="rule-min-qty"
                    className="block text-sm font-medium mb-1"
                  >
                    Min Quantity (gallons)
                  </label>
                  <input
                    id="rule-min-qty"
                    type="number"
                    value={editingRule.min_quantity_gallons ?? ""}
                    onChange={(e) =>
                      setEditingRule({
                        ...editingRule,
                        min_quantity_gallons: e.target.value
                          ? parseInt(e.target.value, 10)
                          : null,
                      })
                    }
                    className="w-full border rounded px-3 py-2"
                    placeholder="Optional"
                  />
                </div>
              </div>
              <div className="flex gap-3 mt-4">
                <Button variant="primary" onClick={handleSaveRule}>
                  {editingIndex !== null ? "Update Rule" : "Add Rule"}
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => {
                    setEditingRule(null);
                    setEditingIndex(null);
                  }}
                >
                  Cancel
                </Button>
              </div>
            </section>
          )}

          {/* Dry-run resolve preview */}
          <section aria-labelledby="resolve-heading" className="border-t pt-6">
            <h2 id="resolve-heading" className="text-lg font-semibold mb-3">
              Pricing Resolve — Dry Run Preview
            </h2>
            <form
              onSubmit={handleResolve}
              className="flex gap-4 items-end flex-wrap mb-4"
            >
              <div>
                <label
                  htmlFor="resolve-account"
                  className="block text-sm font-medium mb-1"
                >
                  Account ID
                </label>
                <input
                  id="resolve-account"
                  type="text"
                  value={resolveRequest.account_id}
                  onChange={(e) =>
                    setResolveRequest({
                      ...resolveRequest,
                      account_id: e.target.value,
                    })
                  }
                  className="border rounded px-3 py-2"
                  placeholder="acc_..."
                />
              </div>
              <div>
                <label
                  htmlFor="resolve-product"
                  className="block text-sm font-medium mb-1"
                >
                  Product Code
                </label>
                <input
                  id="resolve-product"
                  type="text"
                  value={resolveRequest.product_code}
                  onChange={(e) =>
                    setResolveRequest({
                      ...resolveRequest,
                      product_code: e.target.value,
                    })
                  }
                  className="border rounded px-3 py-2"
                  placeholder="ULSD"
                />
              </div>
              <div>
                <label
                  htmlFor="resolve-quantity"
                  className="block text-sm font-medium mb-1"
                >
                  Quantity (gal)
                </label>
                <input
                  id="resolve-quantity"
                  type="number"
                  min={1}
                  value={resolveRequest.quantity_gallons}
                  onChange={(e) =>
                    setResolveRequest({
                      ...resolveRequest,
                      quantity_gallons: parseInt(e.target.value, 10) || 1,
                    })
                  }
                  className="border rounded px-3 py-2 w-28"
                />
              </div>
              <Button
                type="submit"
                variant="secondary"
                disabled={resolving}
                loading={resolving}
              >
                Resolve Price
              </Button>
            </form>

            {resolveError && (
              <div
                role="alert"
                className="bg-error-light border border-error-light text-error-dark p-3 rounded mb-4"
              >
                {resolveError}
              </div>
            )}

            {resolveResult && (
              <div className="border rounded p-4 bg-success-light">
                <h3 className="font-medium mb-2">Resolution Result</h3>
                <dl className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                  <div>
                    <dt className="text-gray-600">Unit Price</dt>
                    <dd className="font-bold">
                      {formatCents(resolveResult.unit_price_cents)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-gray-600">Matched Rule</dt>
                    <dd className="font-mono text-xs">
                      {resolveResult.rule_id}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-gray-600">Scope</dt>
                    <dd className="capitalize">{resolveResult.scope_type}</dd>
                  </div>
                  <div>
                    <dt className="text-gray-600">Cache</dt>
                    <dd>{resolveResult.matched_from_cache ? "Hit" : "Miss"}</dd>
                  </div>
                </dl>
              </div>
            )}
          </section>
        </>
      )}

      {/* Create Price Book modal */}
      {showCreateModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-book-heading"
        >
          <div className="bg-white rounded-lg shadow-lg w-full max-w-md p-6">
            <h2 id="create-book-heading" className="text-lg font-semibold mb-4">
              Create Price Book
            </h2>
            {createError && (
              <div
                role="alert"
                className="bg-error-light border border-error-light text-error-dark p-3 rounded mb-4 text-sm"
              >
                {createError}
              </div>
            )}
            <form onSubmit={handleCreateBook}>
              <div className="mb-4">
                <label
                  htmlFor="create-book-name"
                  className="block text-sm font-medium mb-1"
                >
                  Name
                </label>
                <input
                  id="create-book-name"
                  type="text"
                  value={createName}
                  onChange={(e) => setCreateName(e.target.value)}
                  className="w-full border rounded px-3 py-2"
                  placeholder="e.g. 2026 Q3 Commercial Diesel"
                  // biome-ignore lint/a11y/noAutofocus: focus the first field when the modal opens
                  autoFocus
                />
              </div>
              <div className="mb-4">
                <label
                  htmlFor="create-book-description"
                  className="block text-sm font-medium mb-1"
                >
                  Description
                </label>
                <textarea
                  id="create-book-description"
                  value={createDescription}
                  onChange={(e) => setCreateDescription(e.target.value)}
                  className="w-full border rounded px-3 py-2"
                  rows={3}
                  placeholder="Optional"
                />
              </div>
              <div className="flex justify-end gap-3">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => setShowCreateModal(false)}
                  disabled={creating}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  variant="primary"
                  disabled={creating}
                  loading={creating}
                >
                  Create
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
