/**
 * Board-level banners (R1.4, R2.3, R3.8, R15.6, R21.6): read-only (shadow or
 * past day), live updates paused, degraded validator sources and tray
 * truncation. Every banner carries text and an icon, never colour alone.
 */
import { AlertTriangle, Eye, Info, WifiOff } from "lucide-react";
import type { ReactNode } from "react";

/** R21.6 names per degraded source (backend `SOURCE_CHECKS` keys plus snapshot extras). */
export const DEGRADED_SOURCE_TEXT: Record<string, string> = {
  hos: "HOS data unavailable, HOS checks are warnings only",
  qualification:
    "Driver qualification data unavailable, driver changes are blocked until it returns",
  certification:
    "Truck certification data unavailable, truck changes are blocked until it returns",
  dyed: "Dyed diesel rules unavailable, affected loads are blocked until they return",
  orders: "Order data unavailable, order checks are warnings only",
  other_day:
    "Other days' plans unavailable, cross-day checks are warnings only",
  drivers: "Driver data unavailable, pairing checks are warnings only",
  compartments:
    "Compartment data unavailable, capacity checks are warnings only",
  rules:
    "Product compatibility rules unavailable, compatibility checks are warnings only",
  terminals: "Terminal data unavailable, terminal checks are warnings only",
  terminal_waits:
    "Terminal wait times unavailable, terminal checks are warnings only",
  contracts: "Supply contracts unavailable, contract checks are warnings only",
  executions:
    "Route progress unavailable, started-load checks are warnings only",
  delivery_priorities:
    "Delivery priorities unavailable, orders are sorted by delivery window",
  suggestions: "Agent suggestions unavailable",
  locations:
    "Delivery locations unavailable, stop times and route checks are estimates",
};

export function degradedText(source: string): string {
  return (
    DEGRADED_SOURCE_TEXT[source] ??
    `${source.replace(/_/g, " ")} data unavailable, those checks are warnings only`
  );
}

export const TRUNCATION_TEXT =
  "Showing the 1,000 earliest delivery windows. Filter to narrow the list.";

function Banner({
  tone,
  icon,
  children,
}: {
  tone: "info" | "warning";
  icon: ReactNode;
  children: ReactNode;
}) {
  const toneClass =
    tone === "warning"
      ? "border-warning bg-warning-light text-warning-dark"
      : "border-info bg-info-light text-info-dark";
  return (
    <div
      className={`flex items-start gap-2 border-l-4 px-4 py-2 text-sm ${toneClass}`}
    >
      <span aria-hidden="true" className="mt-0.5">
        {icon}
      </span>
      <div>{children}</div>
    </div>
  );
}

export interface BoardBannersProps {
  readOnlyReason: "shadow" | "past_service_day" | null;
  paused: boolean;
  degradedSources: string[];
  truncated: boolean;
}

export function BoardBanners({
  readOnlyReason,
  paused,
  degradedSources,
  truncated,
}: BoardBannersProps) {
  return (
    <div>
      {readOnlyReason === "shadow" && (
        <div role="status">
          <Banner tone="info" icon={<Eye className="h-4 w-4" />}>
            Preview mode, changes are not saved.
          </Banner>
        </div>
      )}
      {readOnlyReason === "past_service_day" && (
        <div role="status">
          <Banner tone="info" icon={<Eye className="h-4 w-4" />}>
            This day is in the past. The board is read-only.
          </Banner>
        </div>
      )}
      {paused && (
        <div role="status">
          <Banner tone="warning" icon={<WifiOff className="h-4 w-4" />}>
            Live updates paused. The board refreshes every 30 seconds until the
            connection is back.
          </Banner>
        </div>
      )}
      {degradedSources.length > 0 && (
        <div role="status" aria-label="Unavailable data">
          <Banner tone="warning" icon={<AlertTriangle className="h-4 w-4" />}>
            <ul>
              {degradedSources.map((s) => (
                <li key={s}>{degradedText(s)}.</li>
              ))}
            </ul>
          </Banner>
        </div>
      )}
      {truncated && (
        <div role="status">
          <Banner tone="info" icon={<Info className="h-4 w-4" />}>
            {TRUNCATION_TEXT}
          </Banner>
        </div>
      )}
    </div>
  );
}

export default BoardBanners;
