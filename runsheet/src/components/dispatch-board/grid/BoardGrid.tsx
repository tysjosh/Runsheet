/**
 * The lanes grid (design K1, K14.5, K15; R1.6, R2.4–R2.6, R18.4).
 *
 * - One `role="grid"` with roving tabindex: Up/Down between lanes,
 *   Left/Right between cells of a lane, Home/End; Tab leaves the grid.
 * - Vertical windowing by lane height (± 5 rows overscan), so 60 lanes
 *   render a dozen rows; `aria-rowcount`/`aria-rowindex` keep the full count.
 * - `?truck=` scrolls the windowed grid to that lane once (R1.6).
 * - The area under the last lane takes a dragged truck (add lane).
 */
import { CalendarClock } from "lucide-react";
import {
  type KeyboardEvent,
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { LaneView } from "../../../services/dispatchBoardApi";
import { EmptyState } from "../../ui";
import { focusKey, useBoard } from "../BoardContext";
import { shiftWindow } from "../boardTime";
import { useAutoScroll, useDropTarget } from "../dnd/adapter";
import { gridKeyDown, RovingContext, useRovingState } from "../keyboard/roving";
import { Lane } from "./Lane";
import {
  AXIS_HEIGHT,
  COLLAPSED_HEIGHT,
  HEADER_WIDTH,
  LANE_HEIGHT,
  timelineWidth,
} from "./layout";
import { TimeAxis } from "./TimeAxis";

export const OVERSCAN = 5;
/** Viewport assumed before the container is measured (and in jsdom). */
const FALLBACK_VIEWPORT = 720;

export interface BoardGridProps {
  lanes: LaneView[];
  zone: string;
  /** `?truck=` deep link (R1.6). */
  focusTruckId: string | null;
  /** Called with a function that scrolls a lane into the rendered range. */
  registerEnsureVisible: (fn: (truckId: string) => void) => void;
  /** Called with the truck ids currently rendered. */
  onVisibleChange: (truckIds: string[]) => void;
}

/** Rendered index range for a scroll position (pure, for tests). */
export function visibleRange(
  offsets: number[],
  heights: number[],
  scrollTop: number,
  viewport: number,
  overscan = OVERSCAN,
): [number, number] {
  const n = heights.length;
  if (n === 0) return [0, -1];
  let first = 0;
  while (first < n - 1 && offsets[first] + heights[first] <= scrollTop)
    first += 1;
  let last = first;
  while (last < n - 1 && offsets[last + 1] < scrollTop + viewport) last += 1;
  return [Math.max(0, first - overscan), Math.min(n - 1, last + overscan)];
}

function NewLaneArea() {
  const api = useBoard();
  const ref = useRef<HTMLDivElement>(null);
  const over = useDropTarget(ref, {
    target: { target: "new-lane" },
    enabled: true,
    readOnly: api.readOnly,
  });
  const dragging = api.dragItem?.kind === "truck";
  if (!dragging && !over) return <div ref={ref} className="h-6" />;
  return (
    <div
      ref={ref}
      className={`sticky left-0 m-2 flex h-14 items-center justify-center rounded-md border-2 border-dashed text-sm font-medium ${
        over
          ? "border-primary bg-primary-soft text-primary"
          : "border-gray-300 text-gray-600"
      }`}
      style={{ width: `calc(100% - 16px)`, maxWidth: 960 }}
    >
      Drop the truck here to add a lane
    </div>
  );
}

export function BoardGrid({
  lanes,
  zone,
  focusTruckId,
  registerEnsureVisible,
  onVisibleChange,
}: BoardGridProps) {
  const api = useBoard();
  const scrollRef = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const roving = useRovingState(gridRef);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(FALLBACK_VIEWPORT);
  useAutoScroll(scrollRef);

  const window = useMemo(
    () =>
      shiftWindow(
        api.serviceDate,
        api.view.shift,
        api.snapshot.shifts,
        api.timezone,
      ),
    [api.serviceDate, api.view.shift, api.snapshot.shifts, api.timezone],
  );
  const bodyWidth = timelineWidth(window);

  const heights = useMemo(
    () =>
      lanes.map((l) =>
        api.collapsed.has(l.truck_id)
          ? COLLAPSED_HEIGHT
          : LANE_HEIGHT[api.view.density],
      ),
    [lanes, api.collapsed, api.view.density],
  );
  const offsets = useMemo(() => {
    const out: number[] = [];
    let y = 0;
    for (const h of heights) {
      out.push(y);
      y += h;
    }
    return out;
  }, [heights]);
  const total = heights.reduce((s, h) => s + h, 0);
  // K15: the window follows a deferred scroll position, so mounting the lanes
  // that scroll in (each with up to 30 cards) is an interruptible render and
  // doesn't hold up a frame while a drag auto-scrolls; overscan covers the lag.
  const windowTop = useDeferredValue(scrollTop);
  const [first, last] = visibleRange(
    offsets,
    heights,
    Math.max(0, windowTop - AXIS_HEIGHT),
    viewport,
  );
  const rendered = lanes.slice(first, last + 1);
  const laneOrder = useMemo(() => lanes.map((l) => l.truck_id), [lanes]);

  // Measure the viewport where the browser can.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => {
      if (el.clientHeight > 0) setViewport(el.clientHeight);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const scrollToLane = useCallback(
    (truckId: string) => {
      const i = laneOrder.indexOf(truckId);
      const el = scrollRef.current;
      if (i < 0 || !el) return;
      const top = offsets[i];
      const bottom = top + heights[i];
      const view = el.clientHeight || viewport;
      if (top >= scrollTop && bottom <= scrollTop + view - AXIS_HEIGHT) return;
      el.scrollTop = top;
      setScrollTop(top);
    },
    [laneOrder, offsets, heights, scrollTop, viewport],
  );

  useEffect(() => {
    registerEnsureVisible(scrollToLane);
  }, [registerEnsureVisible, scrollToLane]);

  const renderedKey = rendered.map((l) => l.truck_id).join(",");
  useEffect(() => {
    onVisibleChange(rendered.map((l) => l.truck_id));
  }, [renderedKey, onVisibleChange]);

  // `?truck=` deep link: scroll once, when the lane exists (R1.6).
  const scrolledFor = useRef<string | null>(null);
  useEffect(() => {
    if (!focusTruckId || scrolledFor.current === focusTruckId) return;
    if (!laneOrder.includes(focusTruckId)) return;
    scrolledFor.current = focusTruckId;
    const i = laneOrder.indexOf(focusTruckId);
    const el = scrollRef.current;
    if (el) el.scrollTop = offsets[i];
    setScrollTop(offsets[i]);
  }, [focusTruckId, laneOrder, offsets]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const missing = gridKeyDown(e, gridRef.current, roving, laneOrder);
    if (missing) {
      scrollToLane(missing);
      api.requestFocus([focusKey.lane(missing)]);
    }
  };

  if (lanes.length === 0) {
    return (
      <section aria-label="Lanes" className="min-w-0 flex-1 overflow-auto p-4">
        <EmptyState
          icon={<CalendarClock />}
          title="No trucks on the board yet"
          description="Add a truck from the truck tray to start planning."
        />
        <NewLaneArea />
      </section>
    );
  }

  return (
    <section
      aria-label="Lanes"
      ref={scrollRef}
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
      data-scroll-top={scrollTop}
      className="relative min-w-0 flex-1 overflow-auto"
    >
      <RovingContext.Provider value={roving}>
        <div
          ref={gridRef}
          role="grid"
          aria-label="Trucks and loads"
          aria-rowcount={lanes.length + 1}
          aria-multiselectable="true"
          onKeyDown={onKeyDown}
          style={{ minWidth: HEADER_WIDTH + bodyWidth }}
        >
          <TimeAxis
            window={window}
            timeZone={api.timezone}
            zone={zone}
            zoom={api.view.zoom}
            width={bodyWidth}
          />
          <div role="rowgroup">
            <div aria-hidden="true" style={{ height: offsets[first] ?? 0 }} />
            {rendered.map((lane, i) => (
              <Lane
                key={lane.truck_id}
                lane={lane}
                rowIndex={first + i + 2}
                height={heights[first + i]}
                window={window}
                minWidth={bodyWidth}
                collapsed={api.collapsed.has(lane.truck_id)}
              />
            ))}
            <div
              aria-hidden="true"
              style={{
                height: Math.max(
                  0,
                  total - (offsets[last] ?? 0) - (heights[last] ?? 0),
                ),
              }}
            />
          </div>
        </div>
      </RovingContext.Provider>
      <NewLaneArea />
    </section>
  );
}

export default BoardGrid;
