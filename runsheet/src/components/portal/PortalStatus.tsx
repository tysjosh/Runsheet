/**
 * A portal status as hue + icon + the server's label (R14.2, D20).
 *
 * Shared statuses render through `StatusBadge`. The two portal-local invoice
 * statuses (`open`, `partial`) and icon overrides render the same markup here,
 * because Phase 3P doesn't edit the shared badge (follow-up: add the
 * `FileText`/`CircleDollarSign` icons and an `icon` prop to `StatusBadge`).
 */
import {
  CircleDashed,
  CircleDollarSign,
  Clock,
  FileText,
  type LucideIcon,
} from "lucide-react";
import type { StatusKey } from "../../styles/tokens";
import { STATUS_ICONS, StatusBadge } from "../ui/StatusBadge";
import {
  type PortalStatusKey,
  type PortalStatusKind,
  portalStatusStyle,
  portalStatusToken,
} from "./portalStatusMap";

const PORTAL_ICONS: Record<string, LucideIcon> = {
  ...STATUS_ICONS,
  FileText,
  CircleDollarSign,
  Clock,
};

export function PortalBadge({
  status,
  label,
  icon,
  className = "",
}: {
  status: PortalStatusKey;
  label: string;
  icon?: string;
  className?: string;
}) {
  if (!icon && status !== "open" && status !== "partial") {
    return (
      <StatusBadge
        status={status as StatusKey}
        label={label}
        size="md"
        className={className}
      />
    );
  }
  const token = portalStatusToken(status);
  const Icon = PORTAL_ICONS[icon ?? token.icon] ?? CircleDashed;
  return (
    <span
      data-status={status}
      data-icon={icon ?? token.icon}
      className={`inline-flex h-6 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs font-semibold leading-none ${className}`}
      style={{
        backgroundColor: token.bg,
        color: token.fg,
        borderColor: token.border,
        borderStyle: status === "draft" ? "dashed" : "solid",
      }}
    >
      <Icon
        aria-hidden="true"
        className="h-3.5 w-3.5"
        style={{ color: token.fg }}
      />
      <span className={token.strike ? "line-through" : undefined}>{label}</span>
    </span>
  );
}

/** `({kind, code, label})` → the badge from the §11.4 map. */
export default function PortalStatus({
  kind,
  code,
  label,
  className,
}: {
  kind: PortalStatusKind;
  code: string;
  label: string;
  className?: string;
}) {
  const style = portalStatusStyle(kind, code);
  return (
    <PortalBadge
      status={style.key}
      icon={style.icon}
      label={label}
      className={className}
    />
  );
}
