/**
 * Roving tabindex (design K14.5, R18.4). One element per container is in the
 * tab order (`tabIndex=0`); arrow keys move focus between the others. Items
 * are found in DOM order (`[data-roving]`), so the order is what the user
 * sees, including windowed grid rows.
 *
 * - Listboxes (trays): Up/Down, Home/End.
 * - Grid (lanes): Left/Right within a lane row, Up/Down between rows at the
 *   same position (clamped), Home/End to the lane header / last cell.
 */
import {
  createContext,
  type KeyboardEvent,
  type RefObject,
  useCallback,
  useContext,
  useLayoutEffect,
  useState,
} from "react";

export interface Roving {
  active: string | null;
  setActive: (key: string) => void;
}

export const RovingContext = createContext<Roving | null>(null);

/** Props for one roving item. */
export function useRovingItem(key: string) {
  const roving = useContext(RovingContext);
  return {
    tabIndex: roving?.active === key ? 0 : -1,
    "data-roving": "",
    "data-focus-key": key,
    onFocus: () => roving?.setActive(key),
  } as const;
}

/**
 * State for a roving container. After every render, if no rendered item
 * holds the active key (it moved, or its row is windowed out), the first
 * item becomes active so the container stays reachable by Tab.
 */
export function useRovingState(ref: RefObject<HTMLElement | null>): Roving {
  const [active, setActive] = useState<string | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const items = el.querySelectorAll<HTMLElement>("[data-roving]");
    if (items.length === 0) return;
    const has = Array.from(items).some(
      (i) => i.getAttribute("data-focus-key") === active,
    );
    if (!has) {
      setActive(items[0].getAttribute("data-focus-key"));
    }
  });
  return { active, setActive: useCallback((k: string) => setActive(k), []) };
}

function itemsIn(el: Element | null): HTMLElement[] {
  return el
    ? Array.from(el.querySelectorAll<HTMLElement>("[data-roving]"))
    : [];
}

function focusItem(el: HTMLElement | undefined, roving: Roving): boolean {
  if (!el) return false;
  const key = el.getAttribute("data-focus-key");
  if (key) roving.setActive(key);
  el.focus();
  return true;
}

/** Up/Down/Home/End over the items of a listbox. */
export function listKeyDown(
  e: KeyboardEvent<HTMLElement>,
  container: HTMLElement | null,
  roving: Roving,
): void {
  const items = itemsIn(container);
  const current = (e.target as HTMLElement).closest<HTMLElement>(
    "[data-roving]",
  );
  const i = current ? items.indexOf(current) : -1;
  let next: HTMLElement | undefined;
  switch (e.key) {
    case "ArrowDown":
      next = items[Math.min(items.length - 1, i + 1)];
      break;
    case "ArrowUp":
      next = items[Math.max(0, i - 1)];
      break;
    case "Home":
      next = items[0];
      break;
    case "End":
      next = items[items.length - 1];
      break;
    default:
      return;
  }
  e.preventDefault();
  focusItem(next, roving);
}

/**
 * Arrow keys over a grid of lane rows. Returns the truck id of a row that
 * isn't rendered when Up/Down runs off the rendered rows, so the grid can
 * scroll it in and focus it.
 */
export function gridKeyDown(
  e: KeyboardEvent<HTMLElement>,
  grid: HTMLElement | null,
  roving: Roving,
  laneOrder: string[],
): string | null {
  if (!grid) return null;
  const current = (e.target as HTMLElement).closest<HTMLElement>(
    "[data-roving]",
  );
  const row = current?.closest<HTMLElement>("[data-lane-row]");
  if (!current || !row) return null;
  const cells = itemsIn(row);
  const i = cells.indexOf(current);
  switch (e.key) {
    case "ArrowRight":
      e.preventDefault();
      focusItem(cells[Math.min(cells.length - 1, i + 1)], roving);
      return null;
    case "ArrowLeft":
      e.preventDefault();
      focusItem(cells[Math.max(0, i - 1)], roving);
      return null;
    case "Home":
      e.preventDefault();
      focusItem(cells[0], roving);
      return null;
    case "End":
      e.preventDefault();
      focusItem(cells[cells.length - 1], roving);
      return null;
    case "ArrowUp":
    case "ArrowDown": {
      e.preventDefault();
      const truck = row.getAttribute("data-lane-row") ?? "";
      const pos = laneOrder.indexOf(truck);
      const nextTruck =
        laneOrder[e.key === "ArrowDown" ? pos + 1 : pos - 1] ?? null;
      if (!nextTruck) return null;
      const nextRow = grid.querySelector<HTMLElement>(
        `[data-lane-row="${nextTruck.replace(/["\\]/g, "\\$&")}"]`,
      );
      if (!nextRow) return nextTruck;
      const nextCells = itemsIn(nextRow);
      focusItem(nextCells[Math.min(nextCells.length - 1, i)], roving);
      return null;
    }
    default:
      return null;
  }
}
