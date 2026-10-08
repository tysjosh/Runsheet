/**
 * Order tray (R3.1–R3.3, R3.6, R3.8, R4.1, R4.2, R21.3, R21.4). The server
 * order (priority score, then window) is the default sort; the dispatcher can
 * sort by window, customer or product. On-hold orders sit in a collapsed
 * "On hold" group and can't be dragged; their menu offers Release hold.
 * Filters and search dim or highlight cards and never remove them. The tray
 * is also a drop target: a stop dropped here is unassigned (R7.1).
 */
import { useMemo, useRef, useState } from "react";
import type { TrayOrder } from "../../../services/dispatchBoardApi";
import { TRUNCATION_TEXT } from "../BoardBanners";
import { useBoard } from "../BoardContext";
import { useDropTarget } from "../dnd/adapter";
import { listKeyDown, RovingContext, useRovingState } from "../keyboard/roving";
import { OrderCard } from "./OrderCard";
import { trayOrders } from "./trayOrders";

export type TraySort = "priority" | "window" | "customer" | "product";

const SORT_LABEL: Record<TraySort, string> = {
  priority: "Priority",
  window: "Delivery window",
  customer: "Customer",
  product: "Product",
};

function cmp(a: string | null, b: string | null): number {
  if (a === b) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return a.localeCompare(b);
}

/** Client sorts; "priority" keeps the server order (R3.3). */
export function sortOrders(orders: TrayOrder[], sort: TraySort): TrayOrder[] {
  if (sort === "priority") return orders;
  const key = (o: TrayOrder) =>
    sort === "window"
      ? o.delivery_window_start
      : sort === "customer"
        ? (o.customer_name ?? o.customer_id)
        : o.product_code;
  return [...orders].sort(
    (a, b) => cmp(key(a), key(b)) || a.order_id.localeCompare(b.order_id),
  );
}

function Listbox({ label, orders }: { label: string; orders: TrayOrder[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const roving = useRovingState(ref);
  return (
    <RovingContext.Provider value={roving}>
      <div
        ref={ref}
        role="listbox"
        aria-label={label}
        aria-multiselectable="true"
        onKeyDown={(e) => listKeyDown(e, ref.current, roving)}
        className="space-y-2"
      >
        {orders.map((o) => (
          <OrderCard key={o.order_id} order={o} />
        ))}
      </div>
    </RovingContext.Provider>
  );
}

export interface OrderTrayProps {
  /** Stops on lanes, for "All orders are planned (n)". */
  plannedCount: number;
  onGoToOrders: () => void;
}

export function OrderTray({ plannedCount, onGoToOrders }: OrderTrayProps) {
  const api = useBoard();
  const [sort, setSort] = useState<TraySort>("priority");
  const [holdOpen, setHoldOpen] = useState(false);
  const dropRef = useRef<HTMLElement>(null);
  const over = useDropTarget(dropRef, {
    target: { target: "tray" },
    enabled: true,
    readOnly: api.readOnly,
  });
  const { orders_truncated } = api.snapshot.trays;
  const orders = useMemo(
    () => trayOrders(api.snapshot.trays.orders, api.lanesById),
    [api.snapshot.trays.orders, api.lanesById],
  );

  // Orders being assigned are drawn on their lane while the command is pending.
  const moving = useMemo(() => {
    const ids = new Set<string>();
    for (const e of api.optimistic) {
      if (e.item?.kind === "order") for (const id of e.item.ids) ids.add(id);
    }
    return ids;
  }, [api.optimistic]);

  const visible = orders.filter((o) => !moving.has(o.order_id));
  const ready = sortOrders(
    visible.filter((o) => o.block_reason !== "on_hold"),
    sort,
  );
  const onHold = sortOrders(
    visible.filter((o) => o.block_reason === "on_hold"),
    sort,
  );

  return (
    <section
      ref={dropRef}
      aria-label="Order tray"
      data-drop-over={over || undefined}
      className={`flex min-h-0 flex-1 flex-col ${over ? "rounded-md ring-2 ring-primary" : ""}`}
    >
      <div className="flex items-center justify-between gap-2 pb-2">
        <h2 className="text-sm font-semibold text-gray-900">
          Orders ({orders.length})
        </h2>
        <label className="flex items-center gap-1 text-xs text-gray-600">
          Sort
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as TraySort)}
            className="min-h-8 rounded-md border border-gray-300 px-1 text-xs"
          >
            {(Object.keys(SORT_LABEL) as TraySort[]).map((s) => (
              <option key={s} value={s}>
                {SORT_LABEL[s]}
              </option>
            ))}
          </select>
        </label>
      </div>

      {orders_truncated && (
        <p
          role="note"
          className="mb-2 rounded-md border border-warning bg-warning-light px-2 py-1 text-xs text-warning-dark"
        >
          {TRUNCATION_TEXT}
        </p>
      )}

      {over && (
        <p className="mb-2 text-xs font-medium text-primary">
          Drop to unassign
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-auto pr-1">
        {orders.length === 0 && plannedCount === 0 && (
          <div className="text-sm text-gray-600">
            <p>No orders for this day.</p>
            <button
              type="button"
              className="mt-1 min-h-6 font-medium text-primary underline hover:no-underline"
              onClick={onGoToOrders}
            >
              Go to Orders
            </button>
          </div>
        )}
        {orders.length === 0 && plannedCount > 0 && (
          <p className="text-sm text-gray-600">
            All orders are planned ({plannedCount}).
          </p>
        )}
        {ready.length > 0 && <Listbox label="Orders to plan" orders={ready} />}
        {onHold.length > 0 && (
          <div className="mt-3">
            <button
              type="button"
              aria-expanded={holdOpen}
              onClick={() => setHoldOpen((o) => !o)}
              className="min-h-8 text-sm font-medium text-gray-800 hover:underline"
            >
              On hold ({onHold.length})
            </button>
            {holdOpen && (
              <div className="mt-2">
                <Listbox label="On hold orders" orders={onHold} />
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

export default OrderTray;
