"use client";

/**
 * Portal states (D32, R14.16): skeleton rows while loading, an empty state
 * with the next action, a per-section error with Retry, and a slim banner.
 *
 * Loading uses the shared `Skeleton` (its `role="status"` label carries the
 * existing "Loading your …" text). The banner and empty state mirror the
 * shared `InlineBanner`/`EmptyState` at portal sizes (15 px text, 44 px
 * actions on phones, compact padding), since the shared ones are 12 px / 64 px
 * padded staff components.
 */
import { CircleAlert, Info, OctagonAlert } from "lucide-react";
import type { ReactNode } from "react";
import { Skeleton } from "../ui/Skeleton";
import { rowButton } from "./styles";

export function PortalLoading({
  label = "Loading…",
  rows = 3,
}: {
  label?: string;
  rows?: number;
}) {
  return <Skeleton label={label} rows={rows} height={20} className="py-3" />;
}

const TONES = {
  info: { cls: "border-blue-300 bg-blue-50 text-blue-800", Icon: Info },
  warning: {
    cls: "border-amber-300 bg-amber-50 text-amber-800",
    Icon: CircleAlert,
  },
  critical: {
    cls: "border-red-300 bg-red-50 text-red-800",
    Icon: OctagonAlert,
  },
} as const;

/** A slim message strip; `critical` is an alert, the others a status. */
export function PortalBanner({
  tone = "info",
  children,
  action,
  live = true,
  className = "",
}: {
  tone?: keyof typeof TONES;
  children: ReactNode;
  action?: ReactNode;
  /** False when a separate live region already announces the text. */
  live?: boolean;
  className?: string;
}) {
  const { cls, Icon } = TONES[tone];
  return (
    <div
      role={live ? (tone === "critical" ? "alert" : "status") : undefined}
      className={`flex min-h-8 items-center gap-2 rounded-lg border px-3 py-1.5 text-sm font-medium ${cls} ${className}`}
    >
      <Icon aria-hidden="true" className="h-4 w-4 shrink-0" />
      <div className="min-w-0 flex-1">{children}</div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/** A failed section load, announced, with Retry; other sections still render. */
export function PortalSectionError({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <PortalBanner
      tone="critical"
      className="my-2"
      action={
        onRetry ? (
          <button type="button" className={rowButton} onClick={onRetry}>
            Retry
          </button>
        ) : undefined
      }
    >
      {message}
    </PortalBanner>
  );
}

/** Kept for callers of the old name. */
export const PortalLoadError = PortalSectionError;

export function PortalEmpty({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2 px-4 py-8 text-center">
      {icon && (
        <span aria-hidden="true" className="text-slate-400">
          {icon}
        </span>
      )}
      <p className="text-[15px] font-semibold text-text">{title}</p>
      {description && <p className="text-sm text-text-muted">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
