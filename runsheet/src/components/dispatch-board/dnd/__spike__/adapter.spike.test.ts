/**
 * Spike criterion (d): Jest/jsdom can import the Pragmatic-backed adapter and
 * the drop handlers can be invoked directly, without a browser drag.
 */
import {
  insertionIndexFromClientX,
  isDragPayload,
  makeAutoScroll,
  makeDraggable,
  makeLaneDropTarget,
  resolveLaneDrop,
} from "./adapter";

const SLOTS = [
  { left: 100, width: 100 }, // midpoint 150
  { left: 210, width: 100 }, // midpoint 260
  { left: 320, width: 100 }, // midpoint 370
];

describe("dnd spike adapter", () => {
  it("resolves the insertion index from the pointer x", () => {
    expect(insertionIndexFromClientX(50, SLOTS)).toBe(0);
    expect(insertionIndexFromClientX(151, SLOTS)).toBe(1);
    expect(insertionIndexFromClientX(259, SLOTS)).toBe(1);
    expect(insertionIndexFromClientX(261, SLOTS)).toBe(2);
    expect(insertionIndexFromClientX(999, SLOTS)).toBe(3);
    expect(insertionIndexFromClientX(10, [])).toBe(0);
  });

  it("invokes the lane drop handler directly", () => {
    expect(
      resolveLaneDrop({
        sourceData: { kind: "order", ids: ["o1"] },
        input: { clientX: 265 },
        truckId: "T1",
        slots: SLOTS,
      }),
    ).toEqual({
      payload: { kind: "order", ids: ["o1"] },
      truckId: "T1",
      index: 2,
    });
  });

  it("ignores data that is not a board payload", () => {
    expect(isDragPayload({ kind: "file", ids: [] })).toBe(false);
    expect(
      resolveLaneDrop({
        sourceData: { foo: 1 },
        input: { clientX: 0 },
        truckId: "T1",
        slots: SLOTS,
      }),
    ).toBeNull();
  });

  it("registers and cleans up Pragmatic bindings in jsdom", () => {
    const card = document.createElement("div");
    const lane = document.createElement("div");
    const scroller = document.createElement("div");
    document.body.append(card, lane, scroller);
    const cleanups = [
      makeDraggable(card, { kind: "order", ids: ["o1"] }),
      makeLaneDropTarget(lane, {
        truckId: "T1",
        getSlots: () => SLOTS,
        onDrop: jest.fn(),
      }),
      makeAutoScroll(scroller),
    ];
    expect(card.getAttribute("draggable")).toBe("true");
    for (const cleanup of cleanups) cleanup();
    expect(card.getAttribute("draggable")).toBeNull();
  });
});
