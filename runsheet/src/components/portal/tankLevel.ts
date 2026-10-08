/**
 * Tank level status (D23, design §11.5): one rule for the badge, the bar
 * colour and the words, so colour never disagrees with the text.
 *
 * The portal projection exposes no tenant low-tank threshold (checked with
 * `rg -n "low.?(level|tank)|threshold"` over the staff ops components and the
 * fuel backend), so the constants below stand.
 */

import type { PortalTank } from "../../services/portalApi";
import { COLOR } from "../../styles/tokens";
import { days, number, percent, volume } from "./portalFormat";

export type TankLevelStatus = "critical" | "warning" | "ok";

export const LEVEL_RULES = {
  critical: { maxDays: 2, belowPercent: 15 },
  warning: { maxDays: 5, belowPercent: 30 },
} as const;

export const LEVEL_LABELS: Record<TankLevelStatus, string> = {
  critical: "Order now",
  warning: "Order soon",
  ok: "OK",
};

/**
 * Bar fills (≥ 3:1 on the slate-200 track, `portalTokens.test.ts`). Warning
 * uses amber 700: the shared amber 600 dot is 2.5:1 on slate-200.
 */
export const LEVEL_FILL: Record<TankLevelStatus, string> = {
  critical: COLOR.red["600"],
  warning: COLOR.amber["700"],
  ok: COLOR.brand["600"],
};

export const LEVEL_TRACK = COLOR.slate["200"];

/** R7.2 fixed text. */
export const NO_FORECAST_TEXT = "Forecast not available yet";
/** R7.3 fixed text. */
export const STALE_READING_TEXT = "Reading may be out of date";

type LevelInput = Pick<PortalTank, "percent_full" | "forecast">;

export function tankLevel(tank: LevelInput): {
  status: TankLevelStatus;
  label: string;
} {
  const runout = tank.forecast?.days_to_runout;
  const pct = tank.percent_full;
  const has = typeof runout === "number" && Number.isFinite(runout);
  let status: TankLevelStatus = "ok";
  if (
    (has && runout <= LEVEL_RULES.critical.maxDays) ||
    pct < LEVEL_RULES.critical.belowPercent
  ) {
    status = "critical";
  } else if (
    (has && runout <= LEVEL_RULES.warning.maxDays) ||
    pct < LEVEL_RULES.warning.belowPercent
  ) {
    status = "warning";
  }
  return { status, label: LEVEL_LABELS[status] };
}

const RANK: Record<TankLevelStatus, number> = {
  critical: 0,
  warning: 1,
  ok: 2,
};

/** Critical, warning, ok; then days to runout (nulls last); then title. */
export function sortTanks<T extends LevelInput>(
  tanks: readonly T[],
  titleOf: (t: T) => string,
): T[] {
  return [...tanks].sort((a, b) => {
    const r = RANK[tankLevel(a).status] - RANK[tankLevel(b).status];
    if (r !== 0) return r;
    const da = a.forecast?.days_to_runout ?? Number.POSITIVE_INFINITY;
    const db = b.forecast?.days_to_runout ?? Number.POSITIVE_INFINITY;
    if (da !== db) return da - db;
    return titleOf(a).localeCompare(titleOf(b));
  });
}

/** "640 of 2,000 gal, 32 %" (the meter's aria-valuetext). */
export function levelValueText(
  tank: Pick<
    PortalTank,
    "current_level_gallons" | "capacity_gallons" | "percent_full"
  >,
  unit = "gal",
): string {
  return `${number(tank.current_level_gallons)} of ${volume(
    tank.capacity_gallons,
    unit,
  )}, ${percent(tank.percent_full)}`;
}

/**
 * "640 of 2,000 gal · 32 % · empty in about 4 days", or the R7.2 text in place
 * of the runout part. A stale reading adds the R7.3 text.
 */
export function levelLine(tank: PortalTank, unit = "gal"): string {
  const level = `${number(tank.current_level_gallons)} of ${volume(
    tank.capacity_gallons,
    unit,
  )} · ${percent(tank.percent_full)}`;
  const runout = tank.forecast
    ? `empty in about ${days(Math.max(0, tank.forecast.days_to_runout))}`
    : NO_FORECAST_TEXT;
  const stale = tank.reading_stale ? ` · ${STALE_READING_TEXT}` : "";
  return `${level} · ${runout}${stale}`;
}
