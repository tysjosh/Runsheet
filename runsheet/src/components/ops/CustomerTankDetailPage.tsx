"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Badge, Button, EntityLink } from "@/components/ui";
import {
  type CustomerTankWithLinks,
  getCustomerTankWithLinks,
} from "../../services/fuelApi";

interface CustomerTankDetailPageProps {
  customerTankId: string;
  /**
   * Optional in-shell back handler. When omitted (standalone route) the Back
   * button falls back to browser history.
   */
  onBack?: () => void;
}

export default function CustomerTankDetailPage({
  customerTankId,
  onBack,
}: CustomerTankDetailPageProps) {
  const router = useRouter();
  const [tank, setTank] = useState<CustomerTankWithLinks | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchTank = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getCustomerTankWithLinks(customerTankId);
      setTank(data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load tank details",
      );
    } finally {
      setLoading(false);
    }
  }, [customerTankId]);

  useEffect(() => {
    fetchTank();
  }, [fetchTank]);

  const statusVariant = (
    status: string,
  ): "success" | "warning" | "error" | "default" => {
    if (status === "active") return "success";
    if (status === "low") return "warning";
    if (status === "inactive" || status === "decommissioned") return "default";
    return "default";
  };

  const formatGallons = (g: number) =>
    `${g.toLocaleString(undefined, { maximumFractionDigits: 0 })} gal`;

  const formatDateTime = (s?: string | null) =>
    s ? new Date(s).toLocaleString() : "—";

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12">
        <span className="sr-only">Loading tank details...</span>
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

  if (!tank) return null;

  const pctFull =
    tank.capacity_gallons > 0
      ? Math.round((tank.current_level_gallons / tank.capacity_gallons) * 100)
      : null;

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
          <div>
            <h1 className="text-2xl font-bold">Customer Tank</h1>
            <p className="font-mono text-sm text-gray-500">
              {tank.customer_tank_id}
            </p>
          </div>
          <Badge variant={statusVariant(tank.status)}>{tank.status}</Badge>
        </div>
      </header>

      {/* Level summary */}
      <section aria-labelledby="level-heading" className="mb-8">
        <h2 id="level-heading" className="text-lg font-semibold mb-3">
          Level
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Capacity</p>
            <p className="text-2xl font-bold">
              {formatGallons(tank.capacity_gallons)}
            </p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Current Level</p>
            <p className="text-2xl font-bold">
              {formatGallons(tank.current_level_gallons)}
            </p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">% Full</p>
            <p className="text-2xl font-bold">
              {pctFull == null ? "—" : `${pctFull}%`}
            </p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Last Reading</p>
            <p className="text-sm font-medium">
              {formatDateTime(tank.last_reading_at)}
            </p>
          </div>
        </div>
      </section>

      {/* Tank information */}
      <section aria-labelledby="info-heading" className="mb-8">
        <h2 id="info-heading" className="text-lg font-semibold mb-3">
          Tank Information
        </h2>
        <div className="border rounded p-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div>
              <p className="text-sm text-gray-600 mb-1">Customer</p>
              <p className="font-medium">
                <EntityLink
                  type="customer"
                  id={tank.customer_id}
                  link={tank.links?.customer}
                />
              </p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Last Refill Order</p>
              <p className="font-medium">
                <EntityLink
                  type="order"
                  id={tank.last_refill_order_id ?? null}
                  link={tank.links?.last_refill_order}
                />
              </p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Fuel Type</p>
              <p className="font-medium">{tank.fuel_type}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Product Code</p>
              <p className="font-mono text-sm">{tank.fuel_product_code}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Customer Type</p>
              <p className="font-medium">{tank.customer_type}</p>
            </div>
            {tank.use_case && (
              <div>
                <p className="text-sm text-gray-600 mb-1">Use Case</p>
                <p className="font-medium">{tank.use_case}</p>
              </div>
            )}
            <div>
              <p className="text-sm text-gray-600 mb-1">ZIP Code</p>
              <p className="font-medium">{tank.zip_code}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Location</p>
              <p className="font-mono text-sm">
                {tank.location_lat}, {tank.location_lon}
              </p>
            </div>
            {tank.k_factor != null && (
              <div>
                <p className="text-sm text-gray-600 mb-1">K-Factor</p>
                <p className="font-medium">{tank.k_factor}</p>
              </div>
            )}
            <div>
              <p className="text-sm text-gray-600 mb-1">Tank ID</p>
              <p className="font-mono text-sm">{tank.customer_tank_id}</p>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
