/**
 * Customer-facing tank titles (D29, design §11.7, PE2).
 *
 * 1. The staff-set `display_name`, when the projection has one (PE2).
 * 2. Otherwise the label, unless it's the projection's `Tank …xxxxxx`
 *    fallback (an id fragment).
 * 3. Otherwise "{product name} tank", numbered " 2", " 3" by
 *    `customer_tank_id` order when the customer has more than one fallback
 *    tank of that product.
 *
 * Headings never add a "Tank" prefix.
 */
import { productName } from "./portalFormat";

export interface TitledTank {
  customer_tank_id: string;
  label: string;
  product_code: string | null;
  display_name?: string | null;
}

/** The projection's id-fragment fallback label. */
export const FALLBACK_LABEL = /^Tank\s*(…|\.\.\.)/;

export function isFallbackLabel(label: string | null | undefined): boolean {
  return !label || FALLBACK_LABEL.test(label);
}

function productTitle(code: string | null | undefined): string {
  return code ? `${productName(code)} tank` : "Your tank";
}

/** One tank's title without numbering (no list at hand). */
export function tankTitle(tank: TitledTank): string {
  const named = tank.display_name?.trim();
  if (named) return named;
  if (!isFallbackLabel(tank.label)) return tank.label;
  return productTitle(tank.product_code);
}

/** `{customer_tank_id: title}` for a customer's tanks, numbered per product. */
export function tankTitles(tanks: readonly TitledTank[]): Map<string, string> {
  const out = new Map<string, string>();
  const byProduct = new Map<string, TitledTank[]>();
  for (const t of tanks) {
    const title = tankTitle(t);
    out.set(t.customer_tank_id, title);
    if (!t.display_name?.trim() && isFallbackLabel(t.label)) {
      const key = t.product_code ?? "";
      byProduct.set(key, [...(byProduct.get(key) ?? []), t]);
    }
  }
  for (const group of byProduct.values()) {
    if (group.length < 2) continue;
    const sorted = [...group].sort((a, b) =>
      a.customer_tank_id.localeCompare(b.customer_tank_id),
    );
    sorted.forEach((t, i) => {
      if (i > 0) out.set(t.customer_tank_id, `${tankTitle(t)} ${i + 1}`);
    });
  }
  return out;
}

/**
 * Title for an order's tank: the list's title when known, else the label
 * rule with the order's product.
 */
export function orderTankTitle(
  tank: { customer_tank_id: string; label: string } | null,
  productCode: string | null,
  titles?: Map<string, string>,
): string {
  if (!tank) return productTitle(productCode);
  return (
    titles?.get(tank.customer_tank_id) ??
    tankTitle({ ...tank, product_code: productCode })
  );
}
