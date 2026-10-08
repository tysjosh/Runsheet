/**
 * A portal status as hue + icon + the server's label (R14.2, D20).
 *
 * Renders the shared `StatusBadge` (md); `open` and `partial` are shared
 * status tokens and icon overrides use its `icon` prop (task 3.11).
 */
import type { StatusKey } from "../../styles/tokens";
import { StatusBadge } from "../ui/StatusBadge";
import { type PortalStatusKind, portalStatusStyle } from "./portalStatusMap";
export function PortalBadge({
  status,
  label,
  icon,
  className = "",
}: {
  status: StatusKey;
  label: string;
  icon?: string;
  className?: string;
}) {
  return (
    <StatusBadge
      status={status}
      label={label}
      icon={icon}
      size="md"
      className={className}
    />
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
