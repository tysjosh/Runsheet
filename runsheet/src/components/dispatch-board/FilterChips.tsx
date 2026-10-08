/**
 * Filter chips (R4.1): call type, product, priority bucket, window, status
 * and "has warnings". Chips are toggle buttons (`aria-pressed`), so state is
 * never carried by colour alone. Product and priority options come from the
 * snapshot's tray orders.
 */
import type { BoardFilters } from "./viewState";

type ListKey = Exclude<keyof BoardFilters, "has_warnings">;

const FIXED: Record<
  "call_type" | "window" | "status",
  { value: string; label: string }[]
> = {
  call_type: [
    { value: "keep_full", label: "Keep full" },
    { value: "auto_fill", label: "Auto fill" },
    { value: "will_call", label: "Will call" },
    { value: "one_off", label: "One off" },
  ],
  window: [
    { value: "overdue", label: "Overdue" },
    { value: "today", label: "Today" },
    { value: "later", label: "Later" },
  ],
  status: [
    { value: "placed", label: "Placed" },
    { value: "confirmed", label: "Confirmed" },
    { value: "scheduled", label: "Scheduled" },
    { value: "on_hold", label: "On hold" },
  ],
};

const GROUP_LABELS: Record<ListKey, string> = {
  call_type: "Call type",
  product: "Product",
  priority: "Priority",
  window: "Window",
  status: "Status",
};

export interface FilterChipsProps {
  filters: BoardFilters;
  products: string[];
  priorities: string[];
  onChange: (filters: BoardFilters) => void;
}

function Chip({
  label,
  pressed,
  onClick,
}: {
  label: string;
  pressed: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={`min-h-7 rounded-full border px-3 py-0.5 text-xs font-medium ${
        pressed
          ? "border-primary bg-primary text-white"
          : "border-gray-300 bg-white text-gray-700 hover:bg-gray-50"
      }`}
    >
      {label}
    </button>
  );
}

export function FilterChips({
  filters,
  products,
  priorities,
  onChange,
}: FilterChipsProps) {
  const options: Record<ListKey, { value: string; label: string }[]> = {
    call_type: FIXED.call_type,
    product: products.map((p) => ({ value: p, label: p })),
    priority: priorities.map((p) => ({ value: p, label: p })),
    window: FIXED.window,
    status: FIXED.status,
  };
  const toggle = (key: ListKey, value: string) => {
    const current = filters[key];
    const next = current.includes(value)
      ? current.filter((v) => v !== value)
      : [...current, value];
    onChange({ ...filters, [key]: next });
  };
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      {(Object.keys(options) as ListKey[]).map((key) =>
        options[key].length === 0 ? null : (
          <fieldset key={key} className="flex flex-wrap items-center gap-1">
            <legend className="sr-only">{GROUP_LABELS[key]}</legend>
            <span aria-hidden="true" className="mr-1 text-xs text-gray-500">
              {GROUP_LABELS[key]}
            </span>
            {options[key].map((o) => (
              <Chip
                key={o.value}
                label={o.label}
                pressed={filters[key].includes(o.value)}
                onClick={() => toggle(key, o.value)}
              />
            ))}
          </fieldset>
        ),
      )}
      <Chip
        label="Has warnings"
        pressed={filters.has_warnings}
        onClick={() =>
          onChange({ ...filters, has_warnings: !filters.has_warnings })
        }
      />
    </div>
  );
}

export default FilterChips;
