/**
 * Lane geometry (design K1, K15, R2.5, R2.6, R7.4). Timeline places stop
 * cards at their ETA inside the shift window (cards never overlap; a later
 * card is pushed right). Sequence spaces cards evenly in order. Loads run
 * left to right with a return-to-terminal gap. A lane with any stop missing
 * an ETA falls back to Sequence and says so (R2.6).
 */
import type { LaneView } from "../../../services/dispatchBoardApi";
import { HOUR_MS } from "../boardTime";
import type { Density, Zoom } from "../viewState";

export const HEADER_WIDTH = 288;
export const AXIS_HEIGHT = 32;
export const PX_PER_HOUR = 96;
export const LANE_HEIGHT: Record<Density, number> = {
  comfortable: 104,
  compact: 72,
};
export const COLLAPSED_HEIGHT = 44;
export const CARD_WIDTH: Record<Density, number> = {
  comfortable: 120,
  compact: 96,
};
export const CHIP_WIDTH = 72;
const CARD_GAP = 6;
const LOAD_GAP = 32;
const PAD = 8;

export interface CardBox {
  orderId: string;
  left: number;
  ghost: boolean;
}

export interface LoadBox {
  loadId: string;
  left: number;
  width: number;
  /** Cards in stop order (ghosts included). */
  cards: CardBox[];
}

export interface LaneLayout {
  mode: Zoom;
  etasUnavailable: boolean;
  loads: LoadBox[];
  width: number;
}

export interface Ghost {
  orderId: string;
  loadId: string | null;
  index: number | null;
}

export function timelineWidth(window: { start: number; end: number }): number {
  return Math.max(
    PX_PER_HOUR,
    ((window.end - window.start) / HOUR_MS) * PX_PER_HOUR,
  );
}

function toX(ms: number, window: { start: number; end: number }): number {
  return ((ms - window.start) / HOUR_MS) * PX_PER_HOUR;
}

/** Stops of a load with pending ghosts spliced in at their index. */
function stopIds(
  lane: LaneView,
  loadId: string,
  hidden: Set<string>,
  ghosts: Ghost[],
): { id: string; ghost: boolean; eta: number | null }[] {
  const load = lane.loads.find((l) => l.load_id === loadId);
  const out = (load?.stops ?? [])
    .filter((s) => !hidden.has(s.order_id))
    .map((s) => ({
      id: s.order_id,
      ghost: false,
      eta: s.eta ? Date.parse(s.eta) : null,
    }));
  for (const g of ghosts.filter((x) => x.loadId === loadId)) {
    const at = g.index === null ? out.length : Math.min(g.index, out.length);
    out.splice(at, 0, { id: g.orderId, ghost: true, eta: null });
  }
  return out;
}

export function laneLayout(
  lane: LaneView,
  opts: {
    zoom: Zoom;
    density: Density;
    window: { start: number; end: number };
    hidden?: Set<string>;
    ghosts?: Ghost[];
  },
): LaneLayout {
  const hidden = opts.hidden ?? new Set<string>();
  const ghosts = opts.ghosts ?? [];
  const cardW = CARD_WIDTH[opts.density];
  const stops = lane.loads.flatMap((l) => l.stops);
  const etasUnavailable =
    stops.length > 0 &&
    stops.some((s) => !s.eta || Number.isNaN(Date.parse(s.eta)));
  const mode: Zoom =
    opts.zoom === "timeline" && !etasUnavailable ? "timeline" : "sequence";
  const span = timelineWidth(opts.window);
  const loads: LoadBox[] = [];
  let cursor = PAD;
  for (const load of lane.loads) {
    const items = stopIds(lane, load.load_id, hidden, ghosts);
    let left = cursor;
    if (mode === "timeline") {
      const start = load.planned_start
        ? Date.parse(load.planned_start)
        : (items.find((i) => i.eta !== null)?.eta ?? null);
      if (start !== null && !Number.isNaN(start)) {
        left = Math.max(cursor, Math.min(span, toX(start, opts.window)));
      }
    }
    const cards: CardBox[] = [];
    let x = left + CHIP_WIDTH + CARD_GAP;
    for (const it of items) {
      let at = x;
      if (mode === "timeline" && it.eta !== null) {
        at = Math.max(x, toX(it.eta, opts.window) - cardW / 2);
      }
      cards.push({ orderId: it.id, left: at - left, ghost: it.ghost });
      x = at + cardW + CARD_GAP;
    }
    const width = Math.max(CHIP_WIDTH + CARD_GAP + cardW, x - left);
    loads.push({ loadId: load.load_id, left, width, cards });
    cursor = left + width + LOAD_GAP;
  }
  const width = Math.max(mode === "timeline" ? span : 0, cursor + PAD);
  return { mode, etasUnavailable, loads, width };
}
