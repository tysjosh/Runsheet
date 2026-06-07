"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Badge, Button, EntityLink } from "@/components/ui";
import { type DepotReadResponse, getDepot } from "../../services/fuelApi";

interface DepotDetailPageProps {
  depotId: string;
  /** Optional in-shell back handler; falls back to browser history. */
  onBack?: () => void;
}

export default function DepotDetailPage({
  depotId,
  onBack,
}: DepotDetailPageProps) {
  const router = useRouter();
  const [data, setData] = useState<DepotReadResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchDepot = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getDepot(depotId, { expand: ["assets"] });
      setData(res);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load depot details",
      );
    } finally {
      setLoading(false);
    }
  }, [depotId]);

  useEffect(() => {
    fetchDepot();
  }, [fetchDepot]);

  const statusVariant = (status: string): "success" | "warning" | "default" => {
    if (status === "active") return "success";
    if (status === "inactive") return "warning";
    return "default";
  };

  const formatDate = (s?: string | null) =>
    s ? new Date(s).toLocaleDateString() : "—";

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12">
        <span className="sr-only">Loading depot details...</span>
        <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div role="alert" className="p-6">
        <div className="bg-error-light border border-error-light text-error-dark p-4 rounded">
          {error}
        </div>
      </div>
    );
  }

  if (!data) return null;

  const { depot, assigned_assets } = data;

  return (
    <div className="p-6">
      <header className="mb-6">
        <div className="flex items-center gap-4 mb-2">
          <Button
            variant="ghost"
            onClick={() => (onBack ? onBack() : router.back())}
          >
            ← Back
          </Button>
        </div>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold">{depot.name}</h1>
            {depot.is_default && <Badge variant="info">Default</Badge>}
          </div>
          <Badge variant={statusVariant(depot.status)}>{depot.status}</Badge>
        </div>
      </header>

      <section aria-labelledby="info-heading" className="mb-8">
        <h2 id="info-heading" className="text-lg font-semibold mb-3">
          Depot Information
        </h2>
        <div className="border rounded p-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div>
              <p className="text-sm text-gray-600 mb-1">Address</p>
              <p className="font-medium">{depot.address || "—"}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Timezone</p>
              <p className="font-medium">{depot.timezone || "—"}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Location</p>
              <p className="font-mono text-sm">
                {depot.location_lat}, {depot.location_lon}
              </p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Fuel Types Supported</p>
              <div className="flex flex-wrap gap-1.5">
                {depot.fuel_types_supported?.length ? (
                  depot.fuel_types_supported.map((ft) => (
                    <Badge key={ft} variant="default" size="sm">
                      {ft}
                    </Badge>
                  ))
                ) : (
                  <span className="text-gray-500">—</span>
                )}
              </div>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Depot ID</p>
              <p className="font-mono text-sm">{depot.depot_id}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Created</p>
              <p className="font-medium">{formatDate(depot.created_at)}</p>
            </div>
          </div>
        </div>
      </section>

      {/* Assigned assets (expand=assets) */}
      <section aria-labelledby="assets-heading" className="mb-8">
        <h2 id="assets-heading" className="text-lg font-semibold mb-3">
          Assigned Assets
        </h2>
        <div className="border rounded p-6">
          {assigned_assets && assigned_assets.length > 0 ? (
            <ul className="space-y-2">
              {assigned_assets.map((asset) => (
                <li key={asset.asset_id} className="text-sm">
                  <EntityLink
                    type="asset"
                    id={asset.asset_id}
                    label={asset.name ?? undefined}
                  />
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-gray-500 text-sm">
              No assets assigned to this depot.
            </p>
          )}
        </div>
      </section>
    </div>
  );
}
