/**
 * Typed drag payloads and drop-target data (design K14.3). Pragmatic stores
 * both as `Record<string | symbol, unknown>`; these guards narrow them back.
 * The drop target data is a `BoardTarget` (`../intents`) plus a marker, so a
 * drop resolves to the same command a menu or Place mode would send.
 */
import { extractClosestEdge } from "@atlaskit/pragmatic-drag-and-drop-hitbox/closest-edge/extract-closest-edge";
import {
  accepts,
  type BoardItem,
  type BoardTarget,
  type ItemKind,
} from "../intents";

const SOURCE_MARK = "__boardItem";
const TARGET_MARK = "__boardTarget";

const KINDS: ItemKind[] = ["order", "stop", "load", "driver", "truck"];

/** Source data for `draggable({ getInitialData })`. */
export function itemData(item: BoardItem): Record<string, unknown> {
  return {
    [SOURCE_MARK]: true,
    kind: item.kind,
    ids: [...item.ids],
    fromTruckId: item.fromTruckId ?? null,
  };
}

export function isItemData(data: unknown): data is Record<string, unknown> & {
  kind: ItemKind;
  ids: string[];
  fromTruckId: string | null;
} {
  if (!data || typeof data !== "object") return false;
  const d = data as Record<string, unknown>;
  return (
    d[SOURCE_MARK] === true &&
    KINDS.includes(d.kind as ItemKind) &&
    Array.isArray(d.ids) &&
    d.ids.length > 0 &&
    d.ids.every((id) => typeof id === "string")
  );
}

export function toItem(data: unknown): BoardItem | null {
  if (!isItemData(data)) return null;
  return { kind: data.kind, ids: data.ids, fromTruckId: data.fromTruckId };
}

/** Target data for `dropTargetForElements({ getData })`. */
export function targetData(target: BoardTarget): Record<string, unknown> {
  return { [TARGET_MARK]: true, ...target };
}

/**
 * The `BoardTarget` of a drop target's data. A stop card target carries a
 * closest edge (hitbox): the right edge means the slot after the card.
 */
export function toTarget(
  data: Record<string | symbol, unknown> | null | undefined,
): BoardTarget | null {
  if (!data || data[TARGET_MARK] !== true) return null;
  // String keys only: the hitbox edge lives under a symbol.
  const rest = Object.fromEntries(
    Object.entries(data).filter(([k]) => k !== TARGET_MARK),
  );
  const target = rest as unknown as BoardTarget;
  if (target.target === "load" && typeof target.index === "number") {
    const edge = extractClosestEdge(data);
    if (edge === "right") return { ...target, index: target.index + 1 };
  }
  return target;
}

/** `canDrop`: wrong kinds never light a target up. */
export function canDrop(
  sourceData: unknown,
  target: BoardTarget,
  readOnly: boolean,
): boolean {
  if (readOnly) return false;
  const item = toItem(sourceData);
  return item !== null && accepts(item, target.target);
}

/**
 * Insertion slot for a pointer x (Timeline and Sequence alike): the number
 * of stop cards whose horizontal midpoint lies left of the pointer (snap to
 * the nearest gap; there is no free-time placement, K14.3).
 */
export function insertionIndexFromClientX(
  clientX: number,
  rects: readonly { left: number; width: number }[],
): number {
  let index = 0;
  for (const r of rects) {
    if (clientX > r.left + r.width / 2) index += 1;
    else break;
  }
  return index;
}

/** "3 orders · 9,000 gal" for the custom drag preview. */
export function previewLabel(item: BoardItem, gallons: number | null): string {
  const n = item.ids.length;
  const noun: Record<ItemKind, [string, string]> = {
    order: ["order", "orders"],
    stop: ["stop", "stops"],
    load: ["load", "loads"],
    driver: ["driver", "drivers"],
    truck: ["truck", "trucks"],
  };
  const head =
    n === 1 && (item.kind === "driver" || item.kind === "truck")
      ? `${noun[item.kind][0]} ${item.ids[0]}`
      : `${n} ${noun[item.kind][n === 1 ? 0 : 1]}`;
  if (gallons && gallons > 0) {
    return `${head} · ${new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(gallons)} gal`;
  }
  return head;
}
