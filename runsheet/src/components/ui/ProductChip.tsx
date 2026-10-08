/**
 * ProductChip: the API RP 1637 fill-cap colour, its symbol, and the readable
 * product name (R5.4). Raw product codes never appear as primary text.
 *
 *   cap   round symbol only, named for assistive tech (aria-label + title)
 *   chip  cap + name
 *   full  cap + name + code as secondary grey text
 *
 * Unknown codes render the neutral `_unknown` cap with the humanised code.
 */
import { productName } from "../../lib/format";
import { PRODUCT, type ProductToken } from "../../styles/tokens";

export function productToken(code: string | null | undefined): ProductToken {
  return (code && PRODUCT[code as keyof typeof PRODUCT]) || PRODUCT._unknown;
}

export interface ProductChipProps {
  code: string;
  variant?: "cap" | "chip" | "full";
  size?: "sm" | "md";
  className?: string;
}

export function ProductCap({
  code,
  size = "sm",
  decorative = false,
}: {
  code: string;
  size?: "sm" | "md";
  /** True when a visible name sits next to the cap (avoids double reading). */
  decorative?: boolean;
}) {
  const t = productToken(code);
  const name = productName(code);
  const dim = size === "md" ? "h-6 w-6" : "h-5 w-5";
  const font =
    t.symbol.length >= 3
      ? "text-[8px]"
      : t.symbol.length === 2
        ? "text-[9px]"
        : "text-[10px]";
  return (
    <span
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : name}
      aria-hidden={decorative ? true : undefined}
      title={decorative ? undefined : name}
      data-product={code}
      className={`inline-flex ${dim} shrink-0 items-center justify-center rounded-full border-[1.5px] font-bold leading-none ${font}`}
      style={{ backgroundColor: t.bg, color: t.fg, borderColor: t.border }}
    >
      {t.symbol}
    </span>
  );
}

export function ProductChip({
  code,
  variant = "chip",
  size = "sm",
  className = "",
}: ProductChipProps) {
  if (variant === "cap") return <ProductCap code={code} size={size} />;
  const name = productName(code);
  return (
    <span
      className={`inline-flex min-w-0 items-center gap-1.5 ${size === "md" ? "text-sm" : "text-xs"} ${className}`}
      title={variant === "full" ? `${name} (${code})` : name}
    >
      <ProductCap code={code} size={size} decorative />
      <span className="truncate font-medium text-text">{name}</span>
      {variant === "full" && (
        <span className="shrink-0 font-mono text-[11px] text-text-muted">
          {code}
        </span>
      )}
    </span>
  );
}
