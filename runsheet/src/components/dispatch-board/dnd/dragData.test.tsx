/**
 * DnD adapter pure parts and Pragmatic registration in jsdom (plan task 29,
 * design K14.3). Drag gestures themselves are Playwright's job (task 37).
 */
import { attachClosestEdge } from "@atlaskit/pragmatic-drag-and-drop-hitbox/closest-edge/attach-closest-edge";
import { render } from "@testing-library/react";
import { useRef } from "react";
import { intentFor, loadMoveTarget, stopMoveTarget } from "../intents";
import { makeLane, makeLoad } from "../state/testFixtures";
import { useDraggableItem, useDropTarget } from "./adapter";
import {
  canDrop,
  insertionIndexFromClientX,
  itemData,
  previewLabel,
  targetData,
  toItem,
  toTarget,
} from "./dragData";

const lanes = {
  T1: makeLane("T1", 3, {
    loads: [makeLoad("L1", ["A", "B", "C"]), makeLoad("L2", ["D"])],
  }),
  T2: makeLane("T2", 1, { loads: [makeLoad("L3", ["E"])] }),
};

describe("payloads and targets", () => {
  it("round-trips item data and rejects foreign drags", () => {
    const data = itemData({ kind: "stop", ids: ["A"], fromTruckId: "T1" });
    expect(toItem(data)).toEqual({
      kind: "stop",
      ids: ["A"],
      fromTruckId: "T1",
    });
    expect(toItem({ kind: "stop", ids: ["A"] })).toBeNull();
    expect(toItem({ files: [] })).toBeNull();
    expect(toTarget({ target: "lane", truckId: "T1" })).toBeNull();
  });

  it("canDrop rejects wrong kinds and read-only boards", () => {
    const driver = itemData({ kind: "driver", ids: ["D1"] });
    const truck = itemData({ kind: "truck", ids: ["T9"] });
    const order = itemData({ kind: "order", ids: ["O1"] });
    expect(
      canDrop(driver, { target: "driver-slot", truckId: "T1" }, false),
    ).toBe(true);
    expect(
      canDrop(
        driver,
        { target: "load", truckId: "T1", loadId: "L1", index: 0 },
        false,
      ),
    ).toBe(false);
    expect(canDrop(truck, { target: "new-lane" }, false)).toBe(true);
    expect(canDrop(truck, { target: "lane", truckId: "T1" }, false)).toBe(
      false,
    );
    expect(canDrop(order, { target: "tray" }, false)).toBe(false);
    expect(canDrop(order, { target: "lane", truckId: "T1" }, true)).toBe(false);
  });

  it("snaps the pointer to the nearest gap", () => {
    const rects = [
      { left: 100, width: 100 },
      { left: 210, width: 100 },
    ];
    expect(insertionIndexFromClientX(50, rects)).toBe(0);
    expect(insertionIndexFromClientX(151, rects)).toBe(1);
    expect(insertionIndexFromClientX(400, rects)).toBe(2);
  });

  it("a stop card's right edge means the slot after it (hitbox)", () => {
    const el = document.createElement("div");
    el.getBoundingClientRect = () =>
      ({
        left: 0,
        right: 100,
        top: 0,
        bottom: 40,
        width: 100,
        height: 40,
        x: 0,
        y: 0,
      }) as DOMRect;
    const base = targetData({
      target: "load",
      truckId: "T1",
      loadId: "L1",
      index: 1,
    });
    const at = (clientX: number) =>
      toTarget(
        attachClosestEdge(base, {
          element: el,
          input: { clientX, clientY: 20 } as never,
          allowedEdges: ["left", "right"],
        }),
      );
    expect(at(10)).toEqual({
      target: "load",
      truckId: "T1",
      loadId: "L1",
      index: 1,
    });
    expect(at(90)).toEqual({
      target: "load",
      truckId: "T1",
      loadId: "L1",
      index: 2,
    });
  });

  it("labels the preview chip", () => {
    expect(previewLabel({ kind: "order", ids: ["a", "b", "c"] }, 9000)).toBe(
      "3 orders · 9,000 gal",
    );
    expect(previewLabel({ kind: "driver", ids: ["D1"] }, null)).toBe(
      "driver D1",
    );
  });
});

describe("intentFor (one translation for every modality)", () => {
  it.each([
    [
      "order → lane header: best fit",
      { kind: "order", ids: ["O1"] },
      { target: "lane", truckId: "T1" },
      { type: "assign_orders", order_ids: ["O1"], truck_id: "T1", target: {} },
    ],
    [
      "order → slot 1 of L1",
      { kind: "order", ids: ["O1"] },
      { target: "load", truckId: "T1", loadId: "L1", index: 1 },
      {
        type: "assign_orders",
        order_ids: ["O1"],
        truck_id: "T1",
        target: { load_id: "L1", index: 1 },
      },
    ],
    [
      "order → new load",
      { kind: "order", ids: ["O1"] },
      { target: "new-load", truckId: "T2" },
      {
        type: "assign_orders",
        order_ids: ["O1"],
        truck_id: "T2",
        target: { load_id: "new" },
      },
    ],
    [
      "stop A → after C in its own load (engine index counts without A)",
      { kind: "stop", ids: ["A"] },
      { target: "load", truckId: "T1", loadId: "L1", index: 3 },
      {
        type: "move_stops",
        order_ids: ["A"],
        truck_id: "T1",
        target: { load_id: "L1", index: 2 },
      },
    ],
    [
      "stop C → before A",
      { kind: "stop", ids: ["C"] },
      { target: "load", truckId: "T1", loadId: "L1", index: 0 },
      {
        type: "move_stops",
        order_ids: ["C"],
        truck_id: "T1",
        target: { load_id: "L1", index: 0 },
      },
    ],
    [
      "stop → other lane's load body (best position in that load)",
      { kind: "stop", ids: ["A"] },
      { target: "load", truckId: "T2", loadId: "L3", index: null },
      {
        type: "move_stops",
        order_ids: ["A"],
        truck_id: "T2",
        target: { load_id: "L3" },
      },
    ],
    [
      "stop → order tray: unassign",
      { kind: "stop", ids: ["A", "D"] },
      { target: "tray" },
      { type: "unassign_orders", order_ids: ["A", "D"] },
    ],
    [
      "stop → shelf: unassign (the server shelves dispatched orders)",
      { kind: "stop", ids: ["A"] },
      { target: "shelf", truckId: "T1" },
      { type: "unassign_orders", order_ids: ["A"] },
    ],
    [
      "load → other lane header: appended",
      { kind: "load", ids: ["L1"] },
      { target: "lane", truckId: "T2" },
      { type: "move_load", load_id: "L1", truck_id: "T2", index: 1 },
    ],
    [
      "load → own lane header: to the end",
      { kind: "load", ids: ["L1"] },
      { target: "lane", truckId: "T1" },
      { type: "move_load", load_id: "L1", truck_id: "T1", index: 1 },
    ],
    [
      "load L2 → position of L1",
      { kind: "load", ids: ["L2"] },
      { target: "load-position", truckId: "T1", index: 0 },
      { type: "move_load", load_id: "L2", truck_id: "T1", index: 0 },
    ],
    [
      "driver → driver slot",
      { kind: "driver", ids: ["D1"] },
      { target: "driver-slot", truckId: "T2" },
      { type: "pair_driver", truck_id: "T2", driver_id: "D1" },
    ],
    [
      "truck → new lane area",
      { kind: "truck", ids: ["T9"] },
      { target: "new-lane" },
      { type: "add_lane", truck_id: "T9" },
    ],
  ] as const)("%s", (_name, item, target, intent) => {
    expect(intentFor(item as never, target as never, lanes)).toEqual(intent);
  });

  it("returns null for a target that doesn't take the item", () => {
    expect(
      intentFor({ kind: "driver", ids: ["D1"] }, { target: "tray" }, lanes),
    ).toBeNull();
    expect(
      intentFor(
        { kind: "order", ids: [] },
        { target: "lane", truckId: "T1" },
        lanes,
      ),
    ).toBeNull();
  });

  it("menu moves name the same slots a drag would", () => {
    const lane = lanes.T1;
    const run = (id: string, m: "earlier" | "later" | "first" | "last") => {
      const t = stopMoveTarget(lane, id, m);
      return t ? intentFor({ kind: "stop", ids: [id] }, t, lanes) : null;
    };
    expect(run("B", "earlier")).toMatchObject({
      target: { load_id: "L1", index: 0 },
    });
    expect(run("B", "later")).toMatchObject({
      target: { load_id: "L1", index: 2 },
    });
    expect(run("B", "first")).toMatchObject({
      target: { load_id: "L1", index: 0 },
    });
    expect(run("A", "last")).toMatchObject({
      target: { load_id: "L1", index: 2 },
    });
    expect(run("A", "earlier")).toBeNull();
    expect(run("C", "later")).toBeNull();
    const load = (id: string, m: "earlier" | "later") => {
      const t = loadMoveTarget(lane, id, m);
      return t ? intentFor({ kind: "load", ids: [id] }, t, lanes) : null;
    };
    expect(load("L1", "later")).toMatchObject({ index: 1 });
    expect(load("L2", "earlier")).toMatchObject({ index: 0 });
    expect(load("L1", "earlier")).toBeNull();
  });
});

describe("Pragmatic registration in jsdom", () => {
  function Probe({ enabled }: { enabled: boolean }) {
    const ref = useRef<HTMLDivElement>(null);
    const target = useRef<HTMLDivElement>(null);
    useDraggableItem(ref, {
      item: { kind: "order", ids: ["O1"] },
      enabled,
      previewLabel: () => "1 order",
    });
    useDropTarget(target, {
      target: { target: "lane", truckId: "T1" },
      enabled,
      readOnly: false,
    });
    return (
      <>
        <div ref={ref} data-testid="card" />
        <div ref={target} data-testid="lane" />
      </>
    );
  }

  it("registers a draggable and cleans it up", () => {
    const { getByTestId, rerender, unmount } = render(<Probe enabled />);
    expect(getByTestId("card")).toHaveAttribute("draggable", "true");
    rerender(<Probe enabled={false} />);
    expect(getByTestId("card")).not.toHaveAttribute("draggable");
    unmount();
  });
});
