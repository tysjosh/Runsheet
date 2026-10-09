"use client";

/**
 * Customer tank detail at `/dashboard/customers/tanks/:id` (UI revamp task
 * 3.3): compact title row with back, status badge and level; the product by
 * name with its RP 1637 chip (code as secondary text), never the raw code
 * alone; volumes, coordinates and dates through `lib/format`.
 */
import { useRouter } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import {
  EntityLink,
  LoadErrorState,
  PageHeader,
  ProductChip,
  Skeleton,
  StatusBadge,
} from "@/components/ui";
import { dateTime, gallons, humanize, number, pct } from "../../lib/format";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import {
  type CustomerTankWithLinks,
  getCustomerTankWithLinks,
} from "../../services/fuelApi";
import { STATUS_BADGE_CONFIG } from "./CustomerTankPage";

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
  const [loadFailure, setLoadFailure] = useState<LoadFailure | null>(null);

  const fetchTank = useCallback(async () => {
    setLoading(true);
    setLoadFailure(null);
    try {
      const data = await getCustomerTankWithLinks(customerTankId);
      setTank(data);
    } catch (err) {
      setLoadFailure(classifyLoadError(err, "Failed to load tank details"));
    } finally {
      setLoading(false);
    }
  }, [customerTankId]);

  useEffect(() => {
    fetchTank();
  }, [fetchTank]);

  const back = () => (onBack ? onBack() : router.back());

  if (loading) {
    return (
      <div className="p-4">
        <Skeleton rows={6} label="Loading tank details" />
      </div>
    );
  }

  if (loadFailure) {
    return (
      <LoadErrorState
        failure={loadFailure}
        entityLabel="Customer tank"
        entityId={customerTankId}
        onBack={back}
        homeHref="/dashboard/customers"
        homeLabel="Go to Customers"
        onRetry={fetchTank}
      />
    );
  }

  if (!tank) return null;

  const pctFull =
    tank.capacity_gallons > 0
      ? (tank.current_level_gallons / tank.capacity_gallons) * 100
      : null;
  const status =
    STATUS_BADGE_CONFIG[tank.status] ?? STATUS_BADGE_CONFIG.inactive;
  const product = tank.fuel_product_code || tank.fuel_type;

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        host
        title="Customer tank"
        back={{ label: "Back", onClick: back }}
        badge={<StatusBadge status={status.status} label={status.label} />}
        counts={
          <span className="font-mono text-xs text-text-muted">
            {tank.customer_tank_id}
          </span>
        }
      />
      <div className="flex-1 overflow-auto p-4">
        <section aria-labelledby="level-heading" className="mb-6">
          <h2
            id="level-heading"
            className="mb-2 text-sm font-semibold text-text"
          >
            Level
          </h2>
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Fact label="Capacity" value={gallons(tank.capacity_gallons)} />
            <Fact
              label="Current level"
              value={gallons(tank.current_level_gallons)}
            />
            <Fact label="Full" value={pct(pctFull)} />
            <Fact label="Last reading" value={dateTime(tank.last_reading_at)} />
          </dl>
        </section>

        <section aria-labelledby="info-heading" className="mb-6">
          <h2
            id="info-heading"
            className="mb-2 text-sm font-semibold text-text"
          >
            Tank information
          </h2>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-3 rounded-lg border border-slate-200 p-4 text-sm md:grid-cols-2">
            <Row label="Customer">
              <EntityLink
                type="customer"
                id={tank.customer_id}
                link={tank.links?.customer}
              />
            </Row>
            <Row label="Last refill order">
              <EntityLink
                type="order"
                id={tank.last_refill_order_id ?? null}
                link={tank.links?.last_refill_order}
              />
            </Row>
            <Row label="Product">
              <ProductChip code={product} variant="full" />
            </Row>
            <Row label="Customer type">{humanize(tank.customer_type)}</Row>
            {tank.use_case && (
              <Row label="Use case">{humanize(tank.use_case)}</Row>
            )}
            <Row label="ZIP code">{tank.zip_code}</Row>
            <Row label="Location">
              <span className="tabular-nums">
                {number(tank.location_lat, { decimals: 5 })},{" "}
                {number(tank.location_lon, { decimals: 5 })}
              </span>
            </Row>
            {tank.k_factor != null && (
              <Row label="K-factor">
                <span className="tabular-nums">
                  {number(tank.k_factor, { decimals: 4 })}
                </span>
              </Row>
            )}
          </dl>
        </section>
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2">
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums text-text">{value}</dd>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="font-medium text-text">{children}</dd>
    </div>
  );
}
