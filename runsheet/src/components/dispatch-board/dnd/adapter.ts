/**
 * Pragmatic drag and drop adapter (design K14.3). Components register
 * draggables and drop targets with these hooks; the board root registers one
 * monitor that turns a drop into a command through `intentFor`, the same
 * path menus and Place mode use. Hover state is local to each target, so a
 * hover re-renders only the hovered element (K15).
 *
 * Only the grip starts a drag (R20.1): the card body stays a tap target.
 */
import {
  draggable,
  dropTargetForElements,
  monitorForElements,
} from "@atlaskit/pragmatic-drag-and-drop/adapter/element-adapter";
import { combine } from "@atlaskit/pragmatic-drag-and-drop/utils/combine";
import { pointerOutsideOfPreview } from "@atlaskit/pragmatic-drag-and-drop/utils/pointer-outside-of-preview";
import { setCustomNativeDragPreview } from "@atlaskit/pragmatic-drag-and-drop/utils/set-custom-native-drag-preview";
import { autoScrollForElements } from "@atlaskit/pragmatic-drag-and-drop-auto-scroll/element";
import { attachClosestEdge } from "@atlaskit/pragmatic-drag-and-drop-hitbox/closest-edge/attach-closest-edge";
import { type RefObject, useEffect, useRef, useState } from "react";
import type { BoardItem, BoardTarget } from "../intents";
import {
  canDrop,
  insertionIndexFromClientX,
  itemData,
  targetData,
  toItem,
  toTarget,
} from "./dragData";

export interface DraggableOptions {
  item: BoardItem;
  /** The grip; only it starts a drag. */
  handleRef?: RefObject<HTMLElement | null>;
  enabled: boolean;
  /** Text for the custom preview chip ("3 orders · 9,000 gal"). */
  previewLabel: () => string;
}

/** Makes `ref` draggable from its grip. Returns whether it is being dragged. */
export function useDraggableItem(
  ref: RefObject<HTMLElement | null>,
  { item, handleRef, enabled, previewLabel }: DraggableOptions,
): boolean {
  const [dragging, setDragging] = useState(false);
  const latest = useRef({ item, previewLabel });
  latest.current = { item, previewLabel };
  const key = `${item.kind}:${item.ids.join(",")}:${item.fromTruckId ?? ""}`;
  useEffect(() => {
    const element = ref.current;
    if (!element || !enabled) return;
    return draggable({
      element,
      dragHandle: handleRef?.current ?? undefined,
      getInitialData: () => itemData(latest.current.item),
      onGenerateDragPreview: ({ nativeSetDragImage }) => {
        setCustomNativeDragPreview({
          nativeSetDragImage,
          getOffset: pointerOutsideOfPreview({ x: "12px", y: "8px" }),
          render: ({ container }) => {
            const chip = document.createElement("div");
            chip.textContent = latest.current.previewLabel();
            chip.style.cssText =
              "padding:4px 10px;border-radius:9999px;background:#111827;color:#fff;font:600 12px system-ui,sans-serif;white-space:nowrap;";
            container.appendChild(chip);
          },
        });
      },
      onDragStart: () => setDragging(true),
      onDrop: () => setDragging(false),
    });
    // `key` re-registers when the dragged ids change.
  }, [ref, handleRef, enabled, key]);
  return dragging;
}

export interface DropTargetOptions {
  target: BoardTarget;
  enabled: boolean;
  readOnly: boolean;
  /** Stop cards: attach the closest left/right edge (hitbox). */
  edge?: boolean;
  /** Load bodies: resolve the slot from the pointer over these cards. */
  slotSelector?: string;
}

/** Registers a drop target; returns whether a valid drag is over it. */
export function useDropTarget(
  ref: RefObject<HTMLElement | null>,
  { target, enabled, readOnly, edge, slotSelector }: DropTargetOptions,
): boolean {
  const [over, setOver] = useState(false);
  const latest = useRef(target);
  latest.current = target;
  const key = JSON.stringify(target);
  useEffect(() => {
    const element = ref.current;
    if (!element || !enabled) return;
    return dropTargetForElements({
      element,
      canDrop: ({ source }) => canDrop(source.data, latest.current, readOnly),
      getData: ({ input, element: el }) => {
        let t = latest.current;
        if (slotSelector && t.target === "load") {
          const rects = Array.from(el.querySelectorAll(slotSelector)).map((n) =>
            n.getBoundingClientRect(),
          );
          t = { ...t, index: insertionIndexFromClientX(input.clientX, rects) };
        }
        const data = targetData(t);
        return edge
          ? attachClosestEdge(data, {
              element: el,
              input,
              allowedEdges: ["left", "right"],
            })
          : data;
      },
      onDragEnter: () => setOver(true),
      onDragLeave: () => setOver(false),
      onDrop: () => setOver(false),
    });
  }, [ref, enabled, readOnly, edge, slotSelector, key]);
  return over;
}

export interface MonitorHandlers {
  onDragStart?: (item: BoardItem) => void;
  /** Innermost target under the pointer changed (or `null`: none). */
  onTargetChange?: (item: BoardItem, target: BoardTarget | null) => void;
  /** Drop on the innermost target; `null` target = cancelled. */
  onDrop?: (item: BoardItem, target: BoardTarget | null) => void;
}

/** Monitors board drags (one per board). */
export function useBoardMonitor(handlers: MonitorHandlers): void {
  const latest = useRef(handlers);
  latest.current = handlers;
  useEffect(() => {
    const innermost = (targets: { data: Record<string | symbol, unknown> }[]) =>
      toTarget(targets[0]?.data);
    let lastKey = "";
    const report = (
      sourceData: unknown,
      targets: { data: Record<string | symbol, unknown> }[],
    ) => {
      const item = toItem(sourceData);
      if (!item) return;
      const target = innermost(targets);
      // The slot inside a card can change without a target change (edges).
      const k = JSON.stringify(target);
      if (k === lastKey) return;
      lastKey = k;
      latest.current.onTargetChange?.(item, target);
    };
    return monitorForElements({
      canMonitor: ({ source }) => toItem(source.data) !== null,
      onDragStart: ({ source }) => {
        lastKey = "";
        const item = toItem(source.data);
        if (item) latest.current.onDragStart?.(item);
      },
      onDropTargetChange: ({ source, location }) =>
        report(source.data, location.current.dropTargets),
      onDrag: ({ source, location }) =>
        report(source.data, location.current.dropTargets),
      onDrop: ({ source, location }) => {
        lastKey = "";
        const item = toItem(source.data);
        if (item)
          latest.current.onDrop?.(
            item,
            innermost(location.current.dropTargets),
          );
      },
    });
  }, []);
}

/** Auto-scroll for the board's scroll containers (vertical and horizontal). */
export function useAutoScroll(...refs: RefObject<HTMLElement | null>[]): void {
  useEffect(() => {
    const elements = refs
      .map((r) => r.current)
      .filter((e): e is HTMLElement => e !== null);
    if (elements.length === 0) return;
    return combine(
      ...elements.map((element) => autoScrollForElements({ element })),
    );
  }, []);
}
