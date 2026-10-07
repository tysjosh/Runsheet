"use client";

/**
 * Dispatch Board DnD spike board (plan task 1, throwaway). A tray of order
 * cards and a vertically scrolling list of lanes, each lane a horizontally
 * scrolling strip of stops: the nested scroll layout the real grid uses.
 */
import { useEffect, useRef, useState } from "react";
import {
  type LaneDrop,
  makeAutoScroll,
  makeDraggable,
  makeLaneDropTarget,
} from "./adapter";

const LANE_COUNT = 24;
const STOPS_PER_LANE = 14;

function initialLanes(): Record<string, string[]> {
  const lanes: Record<string, string[]> = {};
  for (let l = 0; l < LANE_COUNT; l += 1) {
    lanes[`T${l}`] = Array.from(
      { length: STOPS_PER_LANE },
      (_, s) => `T${l}-s${s}`,
    );
  }
  return lanes;
}

function TrayCard({ id }: { id: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const grip = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!ref.current || !grip.current) return;
    return makeDraggable(
      ref.current,
      { kind: "order", ids: [id] },
      grip.current,
    );
  }, [id]);
  return (
    <div
      ref={ref}
      data-testid={`card-${id}`}
      className="flex items-center gap-2 rounded border bg-white p-2"
    >
      <span
        ref={grip}
        data-testid={`grip-${id}`}
        aria-label={`Drag ${id}`}
        className="cursor-grab select-none px-1"
        style={{ touchAction: "none" }}
      >
        ⠿
      </span>
      {id}
    </div>
  );
}

function Lane({
  truckId,
  stops,
  onDrop,
}: {
  truckId: string;
  stops: string[];
  onDrop: (drop: LaneDrop) => void;
}) {
  const target = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    const el = target.current;
    const sc = scroller.current;
    if (!el || !sc) return;
    const cleanups = [
      makeLaneDropTarget(el, {
        truckId,
        getSlots: () =>
          Array.from(el.querySelectorAll<HTMLElement>("[data-slot]")).map(
            (node) => {
              const r = node.getBoundingClientRect();
              return { left: r.left, width: r.width };
            },
          ),
        onDrop,
        onHover: setHover,
      }),
      makeAutoScroll(sc),
    ];
    return () => {
      for (const c of cleanups) c();
    };
  }, [truckId, onDrop]);
  return (
    <div className="flex h-16 items-center border-b">
      <div className="w-24 shrink-0 px-2 text-sm">{truckId}</div>
      <div
        ref={scroller}
        data-testid={`scroller-${truckId}`}
        className="overflow-x-auto"
        style={{ width: 600 }}
      >
        <div
          ref={target}
          data-testid={`lane-${truckId}`}
          data-order={stops.join(",")}
          data-hover={hover ?? ""}
          className="flex gap-2 p-2"
          style={{ width: "max-content" }}
        >
          {stops.map((s) => (
            <div
              key={s}
              data-slot
              data-testid={`stop-${s}`}
              className="w-28 shrink-0 rounded border bg-slate-50 p-2 text-xs"
            >
              {s}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function SpikeBoard() {
  const [tray, setTray] = useState(["o1", "o2", "o3"]);
  const [lanes, setLanes] = useState(initialLanes);
  const [last, setLast] = useState<LaneDrop | null>(null);
  const grid = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!grid.current) return;
    return makeAutoScroll(grid.current);
  }, []);

  const onDrop = useRef((drop: LaneDrop) => {
    setLast(drop);
    setTray((t) => t.filter((id) => !drop.payload.ids.includes(id)));
    setLanes((current) => {
      const next = { ...current };
      const stops = [...(next[drop.truckId] ?? [])];
      stops.splice(drop.index, 0, ...drop.payload.ids);
      next[drop.truckId] = stops;
      return next;
    });
  }).current;

  return (
    <div className="flex gap-4 p-4">
      <div className="w-40 space-y-2" data-testid="tray">
        {tray.map((id) => (
          <TrayCard key={id} id={id} />
        ))}
      </div>
      <div>
        <div
          ref={grid}
          data-testid="grid"
          className="overflow-y-auto border"
          style={{ height: 360, width: 740 }}
        >
          {Object.entries(lanes).map(([truckId, stops]) => (
            <Lane
              key={truckId}
              truckId={truckId}
              stops={stops}
              onDrop={onDrop}
            />
          ))}
        </div>
        <output data-testid="last-drop">
          {last ? JSON.stringify(last) : ""}
        </output>
      </div>
    </div>
  );
}
