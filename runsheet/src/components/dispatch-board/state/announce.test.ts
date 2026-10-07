/**
 * Announcement strings (design K14.6, R19.1, R14.2).
 */
import type { BoardCommand } from "../../../services/dispatchBoardApi";
import {
  ANOTHER_DISPATCHER,
  actorDisplayName,
  announceBlocked,
  announceCommitted,
  announceConflict,
  announceUndoRefused,
  distinctAnnouncement,
  worstCheck,
} from "./announce";
import {
  makeCheck,
  makeCompartment,
  makeDriver,
  makeLane,
  makeLoad,
} from "./testFixtures";

const meta = {
  client_command_id: "11111111-1111-4111-8111-111111111111",
  expected_lane_versions: {},
  input_modality: "menu" as const,
};

function cmd(c: Record<string, unknown>): BoardCommand {
  return { ...meta, ...c } as BoardCommand;
}

describe("actor names", () => {
  it.each([
    ["ana", "ana"],
    ["  ana  ", "ana"],
    ["", ANOTHER_DISPATCHER],
    [null, ANOTHER_DISPATCHER],
    [undefined, ANOTHER_DISPATCHER],
  ])("%p → %p", (input, expected) => {
    expect(actorDisplayName(input)).toBe(expected);
  });

  it("conflict text names the other dispatcher, or falls back", () => {
    expect(announceConflict("12", "ana")).toBe(
      "Truck 12 was changed by ana. Your change was not applied.",
    );
    expect(announceConflict("12", null)).toBe(
      "Truck 12 was changed by Another dispatcher. Your change was not applied.",
    );
  });
});

describe("committed commands", () => {
  const load = makeLoad("L1", ["O9", "O7", "1042"], {
    allocations: [
      {
        order_id: "1042",
        compartment_id: "c2",
        product_code: "ULSD",
        liters: 9200,
        capacity_liters: 10000,
      },
      {
        order_id: "O9",
        compartment_id: "c1",
        product_code: "ULSD",
        liters: 2000,
        capacity_liters: 10000,
      },
    ],
  });
  load.stops[2].snapshot.product_code = "diesel";
  const lane = makeLane("12", 4, {
    loads: [load],
    compartments: [
      makeCompartment("c1", { position_index: 0 }),
      makeCompartment("c2", { position_index: 1 }),
    ],
    checks: [
      makeCheck({
        scope: { truck_id: "12", order_id: "1042" },
        message: "Delivery window at risk",
      }),
    ],
  });

  it("assignment matches the R19.1 example", () => {
    expect(
      announceCommitted(
        cmd({
          type: "assign_orders",
          order_ids: ["1042"],
          truck_id: "12",
          target: {},
        }),
        [lane],
      ),
    ).toBe(
      "Order 1042, 3,000 gallons diesel, assigned to Truck 12, load 1, stop 3. Compartment 2 is 92% full. Warning: delivery window at risk.",
    );
  });

  it("several orders are counted", () => {
    expect(
      announceCommitted(
        cmd({
          type: "move_stops",
          order_ids: ["O9", "O7"],
          truck_id: "12",
          target: {},
        }),
        [lane],
      ),
    ).toBe("2 orders moved to Truck 12. Warning: delivery window at risk.");
  });

  it.each([
    [
      { type: "unassign_orders", order_ids: ["O1"] },
      "Order O1 returned to the order tray.",
    ],
    [
      { type: "pair_driver", truck_id: "12", driver_id: null },
      "Truck 12 has no driver.",
    ],
    [
      { type: "move_load", load_id: "L1", truck_id: "12", index: 1 },
      "Load moved to Truck 12, position 2.",
    ],
    [{ type: "add_lane", truck_id: "12" }, "Truck 12 added to the board."],
    [
      { type: "remove_lane", truck_id: "12" },
      "Truck 12 removed from the board.",
    ],
    [{ type: "revert", target_command_id: "x" }, "Change undone."],
    [{ type: "reapply", target_command_id: "x" }, "Change redone."],
  ])("%p", (c, text) => {
    expect(announceCommitted(cmd(c), [])).toBe(text);
  });

  it("pairing uses the driver name from the lane", () => {
    const paired = makeLane("12", 5, {
      driver_id: "D1",
      driver: makeDriver("D1", { name: "Sam" }),
    });
    expect(
      announceCommitted(
        cmd({ type: "pair_driver", truck_id: "12", driver_id: "D1" }),
        [paired],
      ),
    ).toBe("Sam paired with Truck 12.");
  });
});

describe("refusals", () => {
  it("block names the first reason and counts the rest", () => {
    const checks = [
      makeCheck({ outcome: "block", message: "Driver CDL expired" }),
      makeCheck({ outcome: "warn" }),
      makeCheck({ outcome: "block", message: "No compatible compartment" }),
    ];
    expect(announceBlocked(checks)).toBe(
      "Not applied. Driver CDL expired. 1 more reason.",
    );
  });

  it.each([
    [
      "changed_by_other",
      false,
      "Can't undo. Another dispatcher changed this truck since.",
    ],
    ["published_since", true, "Can't redo. The truck was published since."],
    [undefined, false, "Can't undo. The board changed since."],
  ])("undo refusal %p", (reason, redo, text) => {
    expect(announceUndoRefused(reason, redo)).toBe(text);
  });

  it("worstCheck prefers the order scope and ignores info", () => {
    const checks = [
      makeCheck({ outcome: "block", scope: { truck_id: "T1" } }),
      makeCheck({
        outcome: "warn",
        scope: { truck_id: "T1", order_id: "O1" },
        message: "mine",
      }),
      makeCheck({ outcome: "info", scope: { truck_id: "T1", order_id: "O1" } }),
    ];
    expect(worstCheck(checks, { orderId: "O1" })?.message).toBe("mine");
    expect(worstCheck(checks)?.outcome).toBe("block");
    expect(worstCheck([makeCheck({ outcome: "info" })])).toBeNull();
  });
});

describe("distinctAnnouncement", () => {
  it("appends a zero-width space to a repeat so it is read again", () => {
    expect(distinctAnnouncement("a", "b")).toBe("b");
    const again = distinctAnnouncement("a", "a");
    expect(again).toBe("a\u200B");
    expect(distinctAnnouncement(again, "a")).toBe("a");
  });
});
