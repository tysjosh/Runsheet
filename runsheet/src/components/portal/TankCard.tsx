/**
 * One tank (R7.1–R7.3): the level as text plus a labelled `<meter>`, a stale
 * reading called out in words (never color only), the forecast or "Forecast
 * not available yet", and the next delivery.
 */

import Link from "next/link";
import type { PortalTank } from "../../services/portalApi";
import {
  formatCalendarDate,
  formatDateTime,
  formatNumber,
  formatWindow,
} from "./format";
import { card, textLink } from "./styles";

export const NO_FORECAST_TEXT = "Forecast not available yet";
/** R7.3 fixed text. */
export const STALE_READING_TEXT = "Reading may be out of date";

/** "1,240 of 2,000 gal (62%)". */
export function levelText(tank: PortalTank, unit = "gal"): string {
  return `${formatNumber(tank.current_level_gallons)} of ${formatNumber(
    tank.capacity_gallons,
  )} ${unit} (${Math.round(tank.percent_full)}%)`;
}

export default function TankCard({
  tank,
  volumeUnit = "gal",
  linkToDetail = true,
  headingLevel = 2,
}: {
  tank: PortalTank;
  volumeUnit?: string;
  linkToDetail?: boolean;
  headingLevel?: 2 | 3;
}) {
  const Heading = headingLevel === 3 ? "h3" : "h2";
  const href = `/portal/tanks/${encodeURIComponent(tank.customer_tank_id)}`;
  return (
    <article className={card} aria-label={`Tank ${tank.label}`}>
      <Heading className="text-base font-semibold text-gray-900">
        {linkToDetail ? (
          <Link href={href} className={textLink}>
            {tank.label}
          </Link>
        ) : (
          tank.label
        )}
      </Heading>
      <p className="text-sm text-gray-700">{tank.product_code}</p>

      <p className="mt-3 text-sm font-medium text-gray-900">
        {levelText(tank, volumeUnit)}
      </p>
      <meter
        className="mt-1 h-3 w-full"
        min={0}
        max={tank.capacity_gallons || 1}
        value={tank.current_level_gallons}
        aria-label={`Fuel level for ${tank.label}`}
      >
        {Math.round(tank.percent_full)}%
      </meter>
      <p className="mt-1 text-sm text-gray-700">
        Last reading: {formatDateTime(tank.last_reading_at)}
      </p>
      {tank.reading_stale && (
        <p className="mt-1 text-sm font-medium text-warning-dark">
          {STALE_READING_TEXT}
        </p>
      )}

      <dl className="mt-3 grid grid-cols-1 gap-1 text-sm">
        <div>
          <dt className="inline font-medium text-gray-700">Forecast: </dt>
          <dd className="inline">
            {tank.forecast
              ? `About ${tank.forecast.days_to_runout} days until empty (around ${formatCalendarDate(
                  tank.forecast.runout_at,
                )})`
              : NO_FORECAST_TEXT}
          </dd>
        </div>
        <div>
          <dt className="inline font-medium text-gray-700">Next delivery: </dt>
          <dd className="inline">
            {tank.next_delivery
              ? `${tank.next_delivery.status_label}, ${formatWindow(
                  tank.next_delivery.window_start,
                  tank.next_delivery.window_end,
                )}`
              : "None scheduled"}
          </dd>
        </div>
      </dl>
    </article>
  );
}
