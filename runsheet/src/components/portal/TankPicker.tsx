"use client";

/**
 * Tank choice in the request dialog (D22): a radio group of 64 px cards
 * (product cap, title, level text) instead of a `<select>`, which can't show
 * a cap. Native radios underneath, so arrow keys move the selection and each
 * card is labelled by its title.
 */
import type { PortalTank } from "../../services/portalApi";
import { ProductCap } from "../ui/ProductChip";
import { number, percent, productName } from "./portalFormat";

export default function TankPicker({
  tanks,
  titles,
  value,
  onChange,
  name,
  labelledBy,
  describedBy,
  invalid,
  disabled,
  unit = "gal",
}: {
  tanks: PortalTank[];
  titles: Map<string, string>;
  value: string;
  onChange: (tankId: string) => void;
  name: string;
  labelledBy: string;
  describedBy?: string;
  invalid?: boolean;
  disabled?: boolean;
  unit?: string;
}) {
  return (
    <div
      role="radiogroup"
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      aria-invalid={invalid || undefined}
      className="grid gap-2"
    >
      {tanks.map((t) => {
        const id = `${name}-${t.customer_tank_id}`;
        const checked = value === t.customer_tank_id;
        const title = titles.get(t.customer_tank_id) ?? t.label;
        return (
          <div key={t.customer_tank_id} className="relative">
            <input
              id={id}
              type="radio"
              name={name}
              value={t.customer_tank_id}
              checked={checked}
              disabled={disabled}
              onChange={() => onChange(t.customer_tank_id)}
              aria-labelledby={`${id}-title`}
              aria-describedby={`${id}-level`}
              className="peer absolute inset-0 h-full w-full cursor-pointer opacity-0 disabled:cursor-not-allowed"
            />
            <label
              htmlFor={id}
              className={`pointer-events-none flex min-h-16 items-center gap-3 rounded-xl border px-3 py-2 peer-focus-visible:ring-2 peer-focus-visible:ring-focus peer-focus-visible:ring-offset-2 peer-disabled:opacity-70 ${
                checked
                  ? "border-primary bg-primary-soft"
                  : "border-slate-300 bg-surface"
              }`}
            >
              <span
                aria-hidden="true"
                className={`inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 ${
                  checked ? "border-primary" : "border-slate-500"
                }`}
              >
                {checked && (
                  <span className="h-2.5 w-2.5 rounded-full bg-primary" />
                )}
              </span>
              <ProductCap code={t.product_code} size="md" decorative />
              <span className="min-w-0 flex-1">
                <span
                  id={`${id}-title`}
                  className="block truncate text-[15px] font-semibold text-text"
                  title={title}
                >
                  {title}
                </span>
                <span
                  id={`${id}-level`}
                  className="block truncate text-sm text-text-muted"
                  title={`${productName(t.product_code)} · ${number(t.current_level_gallons)} of ${number(t.capacity_gallons)} ${unit} · ${percent(t.percent_full)}`}
                >
                  {productName(t.product_code)} ·{" "}
                  {number(t.current_level_gallons)} of{" "}
                  {number(t.capacity_gallons)} {unit} ·{" "}
                  {percent(t.percent_full)}
                </span>
              </span>
            </label>
          </div>
        );
      })}
    </div>
  );
}
