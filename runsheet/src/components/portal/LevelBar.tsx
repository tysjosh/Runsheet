/**
 * The tank level as a meter (D23, R14.8): a 10 px track with a fill in the
 * level status colour, `role="meter"` with min, max, now and a spoken value
 * ("640 of 2,000 gal, 32 %"), named by the tank title.
 */
import type { PortalTank } from "../../services/portalApi";
import {
  LEVEL_FILL,
  LEVEL_TRACK,
  levelValueText,
  type TankLevelStatus,
} from "./tankLevel";

export default function LevelBar({
  tank,
  status,
  labelledBy,
  unit = "gal",
  className = "",
}: {
  tank: Pick<
    PortalTank,
    "current_level_gallons" | "capacity_gallons" | "percent_full"
  >;
  status: TankLevelStatus;
  labelledBy: string;
  unit?: string;
  className?: string;
}) {
  const width = Math.max(0, Math.min(100, tank.percent_full));
  return (
    <div
      role="meter"
      aria-valuemin={0}
      aria-valuemax={tank.capacity_gallons}
      aria-valuenow={tank.current_level_gallons}
      aria-valuetext={levelValueText(tank, unit)}
      aria-labelledby={labelledBy}
      data-level={status}
      className={`h-2.5 w-full overflow-hidden rounded-full ${className}`}
      style={{ backgroundColor: LEVEL_TRACK }}
    >
      <div
        className="h-full rounded-full"
        style={{ width: `${width}%`, backgroundColor: LEVEL_FILL[status] }}
      />
    </div>
  );
}
