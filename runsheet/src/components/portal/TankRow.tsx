"use client";

/**
 * One tank (D19, D23, R14.7, R14.8): product cap, title (D29), level status
 * badge, level bar, level and forecast text, the next delivery, and its own
 * Request delivery action. Used on Home and the Tanks list; `TankSummary` is
 * the detail page's lead card built from the same parts.
 */
import Link from "next/link";
import { useId } from "react";
import type { PortalTank } from "../../services/portalApi";
import { ProductCap } from "../ui/ProductChip";
import { StatusBadge } from "../ui/StatusBadge";
import LevelBar from "./LevelBar";
import { PortalBadge } from "./PortalStatus";
import { window as formatWindow, productName } from "./portalFormat";
import { portalStatusStyle } from "./portalStatusMap";
import { rowButton, textLink } from "./styles";
import { levelLine, tankLevel } from "./tankLevel";

/** Next delivery badge style: the server code when sent, else its label. */
function nextDeliveryStyle(next: NonNullable<PortalTank["next_delivery"]>) {
  if (next.status_code) return portalStatusStyle("order", next.status_code);
  if (next.status_label === "Out for delivery")
    return portalStatusStyle("order", "out_for_delivery");
  if (next.status_label === "Confirmed")
    return portalStatusStyle("order", "confirmed");
  return portalStatusStyle("order", "awaiting_confirmation");
}

export function NextDelivery({ tank }: { tank: PortalTank }) {
  const next = tank.next_delivery;
  if (!next) {
    return (
      <span className="text-sm text-text-muted">No delivery scheduled</span>
    );
  }
  const style = nextDeliveryStyle(next);
  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm text-text-muted">
      <span className="sr-only">Next delivery:</span>
      <PortalBadge
        status={style.key}
        icon={style.icon}
        label={next.status_label}
      />
      <span className="min-w-0">
        {formatWindow(next.window_start, next.window_end)}
      </span>
    </span>
  );
}

export default function TankRow({
  tank,
  title,
  unit = "gal",
  onRequest,
  linkToDetail = true,
  headingLevel = 3,
  showProduct = false,
}: {
  tank: PortalTank;
  title: string;
  unit?: string;
  /** Opens the request dialog with this tank selected (D24). */
  onRequest?: (tankId: string) => void;
  linkToDetail?: boolean;
  headingLevel?: 2 | 3;
  /** Prefix the level line with the product name (desktop rows, detail). */
  showProduct?: boolean;
}) {
  const titleId = useId();
  const level = tankLevel(tank);
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const href = `/portal/tanks/${encodeURIComponent(tank.customer_tank_id)}`;
  return (
    <article
      aria-labelledby={titleId}
      data-tank-row
      className="grid grid-cols-[28px_minmax(0,1fr)_auto] items-center gap-x-2.5 gap-y-1.5 px-3.5 py-3"
    >
      <span className="row-span-2 self-start pt-0.5">
        <ProductCap code={tank.product_code} size="md" />
      </span>
      <Heading
        id={titleId}
        className="min-w-0 truncate text-[15px] font-bold text-text"
      >
        {linkToDetail ? (
          <Link
            href={href}
            className={`${textLink} min-h-11 text-text md:min-h-6`}
          >
            {title}
          </Link>
        ) : (
          title
        )}
      </Heading>
      <StatusBadge status={level.status} label={level.label} size="md" />
      <p className="col-span-2 col-start-2 text-sm text-text-muted">
        {showProduct && (
          <span className="max-md:hidden">
            {productName(tank.product_code)} ·{" "}
          </span>
        )}
        {levelLine(tank, unit)}
      </p>
      <LevelBar
        tank={tank}
        status={level.status}
        labelledBy={titleId}
        unit={unit}
        className="col-span-2 col-start-2"
      />
      <div className="col-span-2 col-start-2 flex flex-wrap items-center justify-between gap-2">
        <NextDelivery tank={tank} />
        {onRequest && (
          <button
            type="button"
            className={rowButton}
            aria-label={`Request delivery for ${title}`}
            onClick={() => onRequest(tank.customer_tank_id)}
          >
            Request delivery
          </button>
        )}
      </div>
    </article>
  );
}
