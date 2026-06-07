/**
 * ProductPicker — searchable fuel-product selector backed by /fuel/products.
 *
 * Replaces free-text product-code entry with the tenant's fuel product
 * catalog so the dispatcher picks a product by its display name instead of
 * memorizing canonical product codes.
 */

"use client";

import { useEffect, useState } from "react";
import { listFuelProducts } from "../../services/fuelApi";
import { SearchableSelect, type SearchableSelectOption } from "../ui";

interface ProductPickerProps {
  value: string | null;
  onChange: (productCode: string) => void;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
  placeholder?: string;
  allowClear?: boolean;
}

export default function ProductPicker({
  value,
  onChange,
  disabled = false,
  id,
  "aria-label": ariaLabel = "Product",
  placeholder = "Select a product…",
  allowClear = false,
}: ProductPickerProps) {
  const [options, setOptions] = useState<SearchableSelectOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError(false);
      try {
        const res = await listFuelProducts();
        if (cancelled) return;
        const rows = Array.isArray(res.items) ? res.items : [];
        setOptions(
          rows.map((p) => ({
            value: p.product_code,
            label: p.display_name || p.product_code,
            sublabel: [p.category ?? "", p.product_code]
              .filter(Boolean)
              .join(" · "),
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
  }, []);

  // Keep an already-selected product visible even if not in the loaded set.
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
      loading={loading}
      disabled={disabled}
      allowClear={allowClear}
      placeholder={placeholder}
      searchPlaceholder="Search products…"
      emptyMessage={loadError ? "Couldn't load products" : "No products found"}
    />
  );
}
