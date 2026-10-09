/**
 * IdentityAvatar: initials on the id's stable identity colour (design.md §2.3).
 * White initials are ≥ 4.5:1 on every identity colour. The label is exposed
 * to assistive tech; the colour is decorative.
 */
import { identityFor, initials } from "../../lib/identity";

export interface IdentityAvatarProps {
  id: string;
  /** Name the initials come from (also the accessible name). */
  label: string;
  size?: "xs" | "sm" | "md";
  /** Explicit colour, e.g. from `assignLaneIdentities`. */
  color?: string;
  className?: string;
}

const SIZES = {
  xs: "h-5 w-5 text-[9px]",
  sm: "h-6 w-6 text-[10px]",
  md: "h-8 w-8 text-xs",
};

export function IdentityAvatar({
  id,
  label,
  size = "sm",
  color,
  className = "",
}: IdentityAvatarProps) {
  const bg = color ?? identityFor(id).hex;
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={`inline-flex shrink-0 items-center justify-center rounded-full font-bold text-white ${SIZES[size]} ${className}`}
      style={{ backgroundColor: bg }}
    >
      <span aria-hidden="true">{initials(label)}</span>
    </span>
  );
}
