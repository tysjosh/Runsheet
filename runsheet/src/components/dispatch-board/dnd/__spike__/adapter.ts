/**
 * Dispatch Board DnD spike (plan task 1, throwaway). A thin adapter over
 * Pragmatic drag and drop, shaped like the planned `dnd/` folder (design
 * K14.3): typed payloads, a lane drop target that resolves an insertion
 * index from `location.current.input.clientX`, and auto-scroll registration.
 * The pure functions are exported so Jest can call them without a browser.
 */
import { combine } from "@atlaskit/pragmatic-drag-and-drop/combine";
import {
  draggable,
  dropTargetForElements,
} from "@atlaskit/pragmatic-drag-and-drop/element/adapter";
import { autoScrollForElements } from "@atlaskit/pragmatic-drag-and-drop-auto-scroll/element";

export type DragKind = "order" | "stop";

export interface DragPayload {
  kind: DragKind;
  ids: string[];
  fromTruckId?: string;
}

export interface SlotRect {
  left: number;
  width: number;
}

export interface LaneDrop {
  payload: DragPayload;
  truckId: string;
  index: number;
}

/** Narrow Pragmatic's `Record<string, unknown>` source data to a payload. */
export function isDragPayload(data: unknown): data is DragPayload {
  if (!data || typeof data !== "object") return false;
  const d = data as Record<string, unknown>;
  return (
    (d.kind === "order" || d.kind === "stop") &&
    Array.isArray(d.ids) &&
    d.ids.every((id) => typeof id === "string")
  );
}

/**
 * Insertion index for a pointer x position: the number of slots whose
 * horizontal midpoint lies left of the pointer (snap to the nearest gap).
 */
export function insertionIndexFromClientX(
  clientX: number,
  slots: readonly SlotRect[],
): number {
  let index = 0;
  for (const slot of slots) {
    if (clientX > slot.left + slot.width / 2) index += 1;
    else break;
  }
  return index;
}

/**
 * The drop handler body, independent of the DOM: what a lane does with a
 * Pragmatic `onDrop` event. Returns `null` for data that isn't a payload.
 */
export function resolveLaneDrop(args: {
  sourceData: unknown;
  input: { clientX: number };
  truckId: string;
  slots: readonly SlotRect[];
}): LaneDrop | null {
  if (!isDragPayload(args.sourceData)) return null;
  return {
    payload: args.sourceData,
    truckId: args.truckId,
    index: insertionIndexFromClientX(args.input.clientX, args.slots),
  };
}

export function makeDraggable(
  element: HTMLElement,
  payload: DragPayload,
  dragHandle?: HTMLElement,
): () => void {
  return draggable({
    element,
    dragHandle,
    getInitialData: () => ({ ...payload }),
  });
}

export function makeLaneDropTarget(
  element: HTMLElement,
  opts: {
    truckId: string;
    getSlots: () => SlotRect[];
    onDrop: (drop: LaneDrop) => void;
    onHover?: (index: number | null) => void;
  },
): () => void {
  return dropTargetForElements({
    element,
    getData: () => ({ truckId: opts.truckId }),
    canDrop: ({ source }) => isDragPayload(source.data),
    onDrag: ({ location }) =>
      opts.onHover?.(
        insertionIndexFromClientX(
          location.current.input.clientX,
          opts.getSlots(),
        ),
      ),
    onDragLeave: () => opts.onHover?.(null),
    onDrop: ({ source, location }) => {
      opts.onHover?.(null);
      const drop = resolveLaneDrop({
        sourceData: source.data,
        input: location.current.input,
        truckId: opts.truckId,
        slots: opts.getSlots(),
      });
      if (drop) opts.onDrop(drop);
    },
  });
}

/** Register one or more scroll containers (outer vertical, inner horizontal). */
export function makeAutoScroll(...elements: HTMLElement[]): () => void {
  return combine(
    ...elements.map((element) => autoScrollForElements({ element })),
  );
}
