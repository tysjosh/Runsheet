/**
 * DriverPicker — searchable driver selector backed by the compliance roster.
 *
 * Loads active drivers once and presents them in a SearchableSelect so the
 * dispatcher picks a driver by name instead of typing a driver ID from
 * memory. Falls back gracefully: if the roster can't be loaded, the selected
 * value is still surfaced so assignment isn't blocked.
 */

"use client";

import { useEffect, useState } from "react";
import { getDrivers } from "../../services/complianceApi";
import { SearchableSelect, type SearchableSelectOption } from "../ui";

interface DriverPickerProps {
  value: string | null;
  onChange: (driverId: string) => void;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
  /** Override the default "Select a driver…" placeholder (e.g. for filters). */
  placeholder?: string;
  /** Show a clear control (used when the picker acts as an optional filter). */
  allowClear?: boolean;
}

export default function DriverPicker({
  value,
  onChange,
  disabled = false,
  id,
  "aria-label": ariaLabel = "Driver",
  placeholder = "Select a driver…",
  allowClear = false,
}: DriverPickerProps) {
  const [options, setOptions] = useState<SearchableSelectOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError(false);
      try {
        // Active drivers only — suspended/expired drivers shouldn't be
        // assignable. Pull a generous page so the full roster is searchable
        // client-side.
        const res = await getDrivers({ status: "active", page: 1, size: 200 });
        if (cancelled) return;
        setOptions(
          res.data.map((d) => ({
            value: d.driver_id,
            label: d.full_name,
            sublabel: `CDL ${d.cdl_class} · ${d.driver_id}`,
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

  // If the roster failed to load but a value is already selected, keep it
  // visible so the user understands what's assigned.
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
      searchPlaceholder="Search drivers by name…"
      emptyMessage={
        loadError ? "Couldn't load drivers" : "No active drivers found"
      }
    />
  );
}
