/**
 * Jest-only mocks shared by the board's RTL suites. Nothing here imports
 * board modules, so `jest.mock` factories can `require` it.
 *
 * - `navigation`: `next/navigation` with a settable `params`.
 * - `socket`: `useDispatchBoardSocket` capturing its handlers.
 * - `pragmatic`: the Pragmatic element adapter, recording every draggable,
 *   drop target and monitor so tests can start drags and invoke drop
 *   handlers directly (Playwright drives real drags in plan task 37).
 */

import { jest } from "@jest/globals";

type Cfg = Record<string, any>;

// No `@types/jest` in this app: tests aren't type-checked, helpers are.
const j = jest;

export const navigation = {
  replace: j.fn(),
  push: j.fn(),
  params: new URLSearchParams(),
};

export const navigationMock = {
  useRouter: () => ({ replace: navigation.replace, push: navigation.push }),
  useSearchParams: () => navigation.params,
};

export const socket: {
  paused: boolean;
  handlers: Record<string, ((...args: any[]) => void) | undefined>;
} = { paused: false, handlers: {} };

export const socketMock = {
  useDispatchBoardSocket: (_date: string | null, handlers: never) => {
    socket.handlers = handlers;
    return {
      state: "connected",
      isConnected: true,
      paused: socket.paused,
      sendPresence: () => true,
    };
  },
};

export const registry: {
  draggables: Cfg[];
  targets: Cfg[];
  monitors: Cfg[];
} = { draggables: [], targets: [], monitors: [] };

function remover(list: Cfg[], cfg: Cfg) {
  return () => {
    const i = list.indexOf(cfg);
    if (i >= 0) list.splice(i, 1);
  };
}

export const pragmaticMock = {
  draggable: (cfg: Cfg) => {
    registry.draggables.push(cfg);
    cfg.element.setAttribute("draggable", "true");
    const off = remover(registry.draggables, cfg);
    return () => {
      off();
      cfg.element.removeAttribute("draggable");
    };
  },
  dropTargetForElements: (cfg: Cfg) => {
    registry.targets.push(cfg);
    return remover(registry.targets, cfg);
  },
  monitorForElements: (cfg: Cfg) => {
    registry.monitors.push(cfg);
    return remover(registry.monitors, cfg);
  },
};

export const autoScrollMock = { autoScrollForElements: () => () => {} };

export function resetMocks() {
  navigation.replace.mockReset();
  navigation.push.mockReset();
  navigation.params = new URLSearchParams();
  socket.paused = false;
  socket.handlers = {};
}

function draggableFor(el: Element): Cfg {
  const cfg = registry.draggables.find((d) => d.element === el);
  if (!cfg) throw new Error("element is not draggable");
  return cfg;
}

function targetFor(el: Element): Cfg {
  const cfg = registry.targets.find((t) => t.element === el);
  if (!cfg) throw new Error("element is not a drop target");
  return cfg;
}

export function isDropTarget(el: Element): boolean {
  return registry.targets.some((t) => t.element === el);
}

/** Source data a drag of `el` would carry. */
export function sourceData(el: Element): Record<string, unknown> {
  return draggableFor(el).getInitialData({});
}

const input = (clientX: number) => ({
  clientX,
  clientY: 0,
  pageX: clientX,
  pageY: 0,
  altKey: false,
  button: 0,
  buttons: 1,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
});

/** Starts a drag of `el`: every monitor's `onDragStart`. */
export function startDrag(el: Element): Record<string, unknown> {
  const cfg = draggableFor(el);
  const data = cfg.getInitialData({});
  const source = { element: el, data };
  const location = {
    initial: { input: input(0), dropTargets: [] },
    current: { input: input(0), dropTargets: [] },
    previous: { dropTargets: [] },
  };
  cfg.onDragStart?.({ source, location });
  for (const m of [...registry.monitors]) {
    if (m.canMonitor && !m.canMonitor({ source, initial: location.initial }))
      continue;
    m.onDragStart?.({ source, location });
  }
  return data;
}

function locationOver(el: Element, data: Record<string, unknown>, x: number) {
  const cfg = targetFor(el);
  const source = { element: document.body, data };
  const canDrop = cfg.canDrop
    ? cfg.canDrop({ source, input: input(x), element: el })
    : true;
  const targets = canDrop
    ? [
        {
          element: el,
          data: cfg.getData
            ? cfg.getData({ input: input(x), element: el, source })
            : {},
          dropEffect: "move",
          isActiveDueToStickiness: false,
        },
      ]
    : [];
  return { cfg, source, canDrop, targets };
}

/** Moves a drag over `el` (monitors' `onDropTargetChange`). */
export function dragOver(
  el: Element,
  data: Record<string, unknown>,
  clientX = 0,
): boolean {
  const { source, canDrop, targets } = locationOver(el, data, clientX);
  const location = {
    initial: { input: input(0), dropTargets: [] },
    current: { input: input(clientX), dropTargets: targets },
    previous: { dropTargets: [] },
  };
  for (const m of [...registry.monitors])
    m.onDropTargetChange?.({ source, location });
  return canDrop;
}

/**
 * Drops `data` on `el`: the target's own `onDrop`, then every monitor's
 * `onDrop` with `el` as the innermost target. Returns whether it accepted.
 */
export function dropOn(
  el: Element,
  data: Record<string, unknown>,
  clientX = 0,
): boolean {
  const { cfg, source, canDrop, targets } = locationOver(el, data, clientX);
  const location = {
    initial: { input: input(0), dropTargets: [] },
    current: { input: input(clientX), dropTargets: targets },
    previous: { dropTargets: [] },
  };
  if (canDrop) cfg.onDrop?.({ source, location, self: targets[0] });
  for (const m of [...registry.monitors]) m.onDrop?.({ source, location });
  return canDrop;
}
