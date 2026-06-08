/**
 * AssetPicker — searchable fleet-asset selector backed by /fleet/assets.
 *
 * Replaces the previously hardcoded ASSETS_BY_JOB_TYPE table (fake TRK-001…
 * seed IDs) with the live fleet roster. The caller passes the job's required
 * `assetType` (derived from the backend job→asset compatibility rule) and the
 * picker loads only matching assets.
 *
 * Optional readiness annotation: callers that track inventory readiness can
 * pass `readinessByAsset` to surface a parts-availability hint per option, and
 * `onAssetsLoaded` to learn which asset IDs were loaded (e.g. to fetch their
 * readiness).
 */

"use client";

import { useEffect, useState } from "react";
import { apiService } from "../../services/api";
import type { ReadinessStatus } from "../../services/inventoryApi";
import type { AssetType } from "../../types/api";
import { SearchableSelect, type SearchableSelectOption } from "../ui";

interface LoadedAsset {
  id: string;
  name: string;
  subtype: string;
}

interface AssetPickerProps {
  /** Coarse asset type the selected job requires (vehicle/vessel/equipment). */
  assetType: AssetType;
  value: string | null;
  onChange: (assetId: string) => void;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
  /** Optional readiness status per asset id, used to annotate options. */
  readinessByAsset?: Record<string, ReadinessStatus>;
  /** Called with the loaded asset ids whenever the roster (re)loads. */
  onAssetsLoaded?: (assetIds: string[]) => void;
  /** Show a clear control (used when the picker acts as an optional filter). */
  allowClear?: boolean;
}

const READINESS_HINT: Record<ReadinessStatus, string> = {
  ready: "● parts ready",
  warning: "● low stock",
  critical: "● critical shortage",
  blocked: "● blocked",
};

export default function AssetPicker({
  assetType,
  value,
  onChange,
  disabled = false,
  id,
  "aria-label": ariaLabel = "Asset",
  readinessByAsset,
  onAssetsLoaded,
  allowClear = false,
}: AssetPickerProps) {
  const [assets, setAssets] = useState<LoadedAsset[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  // Server-side typeahead: debounce the typed query and pass it to the fleet
  // endpoint so matches beyond the loaded set are found (large fleets).
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
        const res = await apiService.getAssets({
          asset_type: assetType,
          search: debouncedQuery.trim() || undefined,
        });
        if (cancelled) return;
        // Guard against a null/non-array data payload (real backends sometimes
        // return data: null) and against assets missing optional fields.
        const rows = Array.isArray(res.data) ? res.data : [];
        const loaded: LoadedAsset[] = rows.map((a) => ({
          id: a.id,
          name: a.name || a.plateNumber || a.id,
          subtype: a.assetSubtype ?? "",
        }));
        setAssets(loaded);
        onAssetsLoaded?.(loaded.map((a) => a.id));
      } catch {
        if (!cancelled) {
          setAssets([]);
          setLoadError(true);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [assetType, debouncedQuery, onAssetsLoaded]);

  const options: SearchableSelectOption[] = assets.map((a) => {
    const readiness = readinessByAsset?.[a.id];
    const subtypeLabel = (a.subtype ?? "").replace(/_/g, " ").trim();
    // Compose "subtype · id · readiness", dropping any empty segments so a
    // missing subtype doesn't leave a dangling separator.
    const sublabel = [
      subtypeLabel,
      a.id,
      readiness ? READINESS_HINT[readiness] : "",
    ]
      .filter(Boolean)
      .join(" · ");
    return { value: a.id, label: a.name, sublabel };
  });

  // Keep an already-selected asset visible even if it's not in the loaded set.
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
      placeholder="— None —"
      searchPlaceholder="Search assets…"
      emptyMessage={
        loadError ? "Couldn't load assets" : "No compatible assets found"
      }
    />
  );
}
