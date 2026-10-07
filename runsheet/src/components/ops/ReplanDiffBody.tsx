/**
 * Structured `ReplanDiff` body (Req 2.5.3): added, removed, reordered and
 * reassigned stops, quantity changes and ETA shifts as collapsible sections.
 *
 * Extracted from `FuelDistributionPage` so the Dispatch Board's suggestion
 * diff (dispatch-board R16.2, plan task 35) renders the same vocabulary.
 */
import { ChevronDown, ChevronUp } from "lucide-react";
import type React from "react";
import { useState } from "react";
import type { ReplanDiff } from "../../services/fuelApi";

interface ReplanDiffSectionProps<T> {
  title: string;
  items: T[];
  emptyLabel?: string;
  render: (item: T, index: number) => React.ReactNode;
  initiallyOpen?: boolean;
}

function ReplanDiffSection<T>({
  title,
  items,
  emptyLabel,
  render,
  initiallyOpen = false,
}: ReplanDiffSectionProps<T>) {
  const [open, setOpen] = useState(initiallyOpen && items.length > 0);
  const isEmpty = items.length === 0;

  return (
    <div className="border border-gray-100 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => !isEmpty && setOpen((o) => !o)}
        disabled={isEmpty}
        className={`w-full flex items-center justify-between px-4 py-2.5 text-sm font-medium transition-colors ${
          isEmpty
            ? "text-gray-500 cursor-default"
            : "text-primary hover:bg-gray-50"
        }`}
        aria-expanded={open}
      >
        <span className="flex items-center gap-2">
          {title}
          <span
            className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
              isEmpty
                ? "bg-gray-50 text-gray-500"
                : "bg-info-light text-info-dark"
            }`}
          >
            {items.length}
          </span>
        </span>
        {!isEmpty &&
          (open ? (
            <ChevronUp className="w-4 h-4 text-gray-500" />
          ) : (
            <ChevronDown className="w-4 h-4 text-gray-500" />
          ))}
      </button>
      {open && !isEmpty && (
        <div className="border-t border-gray-100 divide-y divide-gray-100">
          {items.map((item, i) => (
            <div key={i} className="px-4 py-2 text-xs text-gray-700">
              {render(item, i)}
            </div>
          ))}
        </div>
      )}
      {isEmpty && emptyLabel && (
        <div className="px-4 py-2 text-[11px] text-gray-500">{emptyLabel}</div>
      )}
    </div>
  );
}

export function ReplanDiffBody({ diff }: { diff: ReplanDiff }) {
  return (
    <div className="space-y-2">
      <ReplanDiffSection
        title="Added stops"
        items={diff.added_stops ?? []}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-primary">{s.stop_id}</span>
            <span className="text-gray-500">
              index {s.index}
              {s.gallons != null ? ` · ${s.gallons.toFixed(0)} gal` : ""}
              {s.product_code ? ` · ${s.product_code}` : ""}
              {s.eta ? ` · ETA ${new Date(s.eta).toLocaleString()}` : ""}
            </span>
          </div>
        )}
        initiallyOpen
      />
      <ReplanDiffSection
        title="Removed stops"
        items={diff.removed_stops ?? []}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-primary">{s.stop_id}</span>
            <span className="text-gray-500">
              was index {s.index}
              {s.gallons != null ? ` · ${s.gallons.toFixed(0)} gal` : ""}
            </span>
          </div>
        )}
        initiallyOpen
      />
      <ReplanDiffSection
        title="Reordered stops"
        items={diff.reordered_stops ?? []}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-primary">{s.stop_id}</span>
            <span className="text-gray-500">
              {s.before_index} → {s.after_index}
            </span>
          </div>
        )}
        initiallyOpen
      />
      <ReplanDiffSection
        title="Reassigned stops"
        items={diff.reassigned_stops ?? []}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-primary">{s.stop_id}</span>
            <span className="text-gray-500">
              {s.from_truck_id} → {s.to_truck_id}
            </span>
          </div>
        )}
      />
      <ReplanDiffSection
        title="Quantity changes"
        items={diff.quantity_changes ?? []}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-primary">{s.stop_id}</span>
            <span className="text-gray-500">
              {s.before_gallons.toFixed(0)} → {s.after_gallons.toFixed(0)} gal
              {s.product_code ? ` · ${s.product_code}` : ""}
            </span>
          </div>
        )}
      />
      <ReplanDiffSection
        title="ETA shifts"
        items={diff.eta_shifts ?? []}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-primary">{s.stop_id}</span>
            <span className="text-gray-500">
              {new Date(s.before_eta).toLocaleString()} →{" "}
              {new Date(s.after_eta).toLocaleString()} (
              {s.shift_minutes >= 0 ? "+" : ""}
              {s.shift_minutes.toFixed(0)} min)
            </span>
          </div>
        )}
      />
    </div>
  );
}

export default ReplanDiffBody;
