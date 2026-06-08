/**
 * CustomerPicker — searchable customer selector backed by /commerce/customers.
 *
 * Replaces free-text customer-ID entry with the live customer roster so the
 * dispatcher picks a customer by name. Loads the first page once and filters
 * client-side.
 */

"use client";

import { useEffect, useState } from "react";
import { getCustomers } from "../../services/commerceApi";
import { SearchableSelect, type SearchableSelectOption } from "../ui";

interface CustomerPickerProps {
  value: string | null;
  onChange: (customerId: string) => void;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
  placeholder?: string;
  allowClear?: boolean;
}

export default function CustomerPicker({
  value,
  onChange,
  disabled = false,
  id,
  "aria-label": ariaLabel = "Customer",
  placeholder = "Select a customer…",
  allowClear = false,
}: CustomerPickerProps) {
  const [options, setOptions] = useState<SearchableSelectOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  // Server-side typeahead: the typed query is debounced and sent to the
  // customers endpoint so the picker can surface matches beyond the first
  // page (the roster can exceed the page size).
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 250);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError(false);
      try {
        const res = await getCustomers({
          status: "active",
          limit: 50,
          search: debouncedQuery.trim() || undefined,
        });
        if (cancelled) return;
        const rows = Array.isArray(res.data) ? res.data : [];
        setOptions(
          rows.map((c) => ({
            value: c.customer_id,
            label: c.display_name || c.customer_id,
            sublabel: c.customer_id,
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
  }, [debouncedQuery]);

  // Keep an already-selected customer visible even if not in the loaded set.
  const mergedOptions =
    value && !options.some((o) => o.value === value)
      ? [{ value, label: value }, ...options]
      : options;

  return (
    <SearchableSelect
      id={id}
      aria-label={ariaLabel}
      options={mergedOptions}
      value={value}
      onChange={onChange}
      onSearchChange={setQuery}
      serverFiltered
      loading={loading}
      disabled={disabled}
      allowClear={allowClear}
      placeholder={placeholder}
      searchPlaceholder="Search customers by name…"
      emptyMessage={
        loadError ? "Couldn't load customers" : "No customers found"
      }
    />
  );
}
