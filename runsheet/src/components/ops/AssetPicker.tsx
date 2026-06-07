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
}: AssetPickerProps) {
  const [assets, setAssets] = useState<LoadedAsset[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError(false);
      try {
        const res = await apiService.getAssets({ asset_type: assetType });
        if (cancelled) return;
        const loaded: LoadedAsset[] = res.data.map((a) => ({
          id: a.id,
          name: a.name || a.plateNumber || a.id,
          subtype: a.assetSubtype,
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
  }, [assetType, onAssetsLoaded]);

  const options: SearchableSelectOption[] = assets.map((a) => {
    const readiness = readinessByAsset?.[a.id];
    const subtypeLabel = a.subtype.replace(/_/g, " ");
    const sublabel = readiness
      ? `${subtypeLabel} · ${a.id} · ${READINESS_HINT[readiness]}`
      : `${subtypeLabel} · ${a.id}`;
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
      loading={loading}
      disabled={disabled}
      placeholder="— None —"
      searchPlaceholder="Search assets…"
      emptyMessage={
        loadError ? "Couldn't load assets" : "No compatible assets found"
      }
    />
  );
}
