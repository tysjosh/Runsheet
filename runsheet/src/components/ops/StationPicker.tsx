/**
 * StationPicker — searchable fuel-station selector backed by /fuel/stations.
 *
 * Replaces free-text station-ID entry with the live station roster so the
 * dispatcher picks a station by name. Loads once and filters client-side.
 */

"use client";

import { useEffect, useState } from "react";
import { getStations } from "../../services/fuelApi";
import { SearchableSelect, type SearchableSelectOption } from "../ui";

interface StationPickerProps {
  value: string | null;
  onChange: (stationId: string) => void;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
}

export default function StationPicker({
  value,
  onChange,
  disabled = false,
  id,
  "aria-label": ariaLabel = "Station",
}: StationPickerProps) {
  const [options, setOptions] = useState<SearchableSelectOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError(false);
      try {
        const res = await getStations({ page: 1, size: 200 });
        if (cancelled) return;
        const rows = Array.isArray(res.data) ? res.data : [];
        setOptions(
          rows.map((s) => ({
            value: s.station_id,
            label: s.name || s.station_id,
            sublabel: [s.location_name ?? s.fuel_type ?? "", s.station_id]
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

  // Keep an already-selected station visible even if it's not in the set.
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
      placeholder="Select a station…"
      searchPlaceholder="Search stations by name…"
      emptyMessage={loadError ? "Couldn't load stations" : "No stations found"}
    />
  );
}
