/**
 * InlineBanner: a slim 32 px strip for an actionable message (R4.5). Static
 * warnings belong in a header count chip; use this only when there is
 * something to do, and render nothing when inactive.
 */
import { CircleAlert, Info, OctagonAlert } from "lucide-react";
import type { ReactNode } from "react";

export interface InlineBannerProps {
  tone?: "info" | "warning" | "critical";
  children: ReactNode;
  action?: ReactNode;
  className?: string;
  /** `touch`: at least 44 px tall with 14 px text, for phones (R14.19). Additive (3.11). */
  size?: "default" | "touch";
}

const TONES = {
  info: { cls: "bg-blue-50 text-blue-800 border-blue-300", Icon: Info },
  warning: {
    cls: "bg-amber-50 text-amber-800 border-amber-300",
    Icon: CircleAlert,
  },
  critical: {
    cls: "bg-red-50 text-red-800 border-red-300",
    Icon: OctagonAlert,
  },
};

export function InlineBanner({
  tone = "info",
  children,
  action,
  className = "",
  size = "default",
}: InlineBannerProps) {
  const { cls, Icon } = TONES[tone];
  return (
    <div
      role={tone === "critical" ? "alert" : "status"}
      className={`flex ${size === "touch" ? "min-h-11 py-2 text-sm" : "min-h-8 py-1 text-xs"} items-center gap-2 rounded-md border px-3 font-medium ${cls} ${className}`}
    >
      <Icon aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
      <div className="min-w-0 flex-1">{children}</div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
