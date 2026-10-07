/**
 * Hour ticks across the shift window, in the tenant time zone (R2.4, R2.5,
 * R2.7). In Sequence layout stops aren't placed by time, so the axis says so.
 */
import { formatTime, HOUR_MS } from "../boardTime";
import type { Zoom } from "../viewState";
import { AXIS_HEIGHT, HEADER_WIDTH, PX_PER_HOUR } from "./layout";

export interface TimeAxisProps {
  window: { start: number; end: number };
  timeZone: string;
  zone: string;
  zoom: Zoom;
  width: number;
}

export function TimeAxis({
  window,
  timeZone,
  zone,
  zoom,
  width,
}: TimeAxisProps) {
  const hours = Math.round((window.end - window.start) / HOUR_MS);
  return (
    <div role="rowgroup" className="sticky top-0 z-20">
      <div
        role="row"
        tabIndex={-1}
        aria-rowindex={1}
        className="flex border-b border-gray-200 bg-gray-50"
        style={{ height: AXIS_HEIGHT }}
      >
        <div
          role="columnheader"
          tabIndex={-1}
          className="sticky left-0 z-10 flex items-center border-r border-gray-200 bg-gray-50 px-3 text-xs font-medium text-gray-700"
          style={{ width: HEADER_WIDTH }}
        >
          Truck and driver
        </div>
        <div
          role="columnheader"
          tabIndex={-1}
          className="relative text-[11px] text-gray-600"
          style={{ width }}
        >
          {zoom === "sequence" ? (
            <span className="absolute left-2 top-2">Stops in order</span>
          ) : (
            <>
              <span className="sr-only">Schedule, times in {zone}</span>
              {Array.from({ length: hours + 1 }, (_, i) => (
                <span
                  key={i}
                  aria-hidden="true"
                  className="absolute top-2 -translate-x-1/2 whitespace-nowrap"
                  style={{ left: i * PX_PER_HOUR + (i === 0 ? 24 : 0) }}
                >
                  {formatTime(window.start + i * HOUR_MS, timeZone)}
                </span>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default TimeAxis;
