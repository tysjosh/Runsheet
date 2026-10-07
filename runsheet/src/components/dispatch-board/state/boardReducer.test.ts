/**
 * Table-driven tests for the board reducer (design K14.2, K10.5, R15.7).
 */
import {
  type BoardAction,
  type BoardState,
  boardReducer,
  initialBoardState,
  isLanePending,
  laneOfDriver,
  laneOfLoad,
  laneOfOrder,
  lanesInOrder,
  UNDO_LIMIT,
} from "./boardReducer";
import { makeLane, makeLoad, makeSnapshot } from "./testFixtures";

function run(actions: BoardAction[], from: BoardState = initialBoardState) {
  return actions.reduce(boardReducer, from);
}

const loaded = (lanes = [makeLane("T1", 3), makeLane("T2", 5)]) =>
  run([
    {
      type: "snapshotLoaded",
      snapshot: makeSnapshot({ lanes, draft_version: 9 }),
    },
  ]);

describe("higher version wins", () => {
  it.each([
    ["lower version is ignored", 2, 3],
    ["equal version replaces (fresh checks, no bump)", 3, 3],
    ["higher version replaces", 4, 4],
  ])("%s", (_name, incoming, expected) => {
    const state = run(
      [
        {
          type: "lanesReceived",
          source: "socket",
          lanes: [makeLane("T1", incoming, { checks_stale: incoming === 3 })],
        },
      ],
      loaded(),
    );
    expect(state.lanesById.T1.version).toBe(expected);
    if (incoming === 3) expect(state.lanesById.T1.checks_stale).toBe(true);
  });

  it.each(["fetch", "socket", "command"] as const)(
    "applies to %s payloads alike",
    (source) => {
      const state = run(
        [{ type: "lanesReceived", source, lanes: [makeLane("T2", 4)] }],
        loaded(),
      );
      expect(state.lanesById.T2.version).toBe(5);
    },
  );

  it("an older full snapshot never rolls back a newer socket lane", () => {
    const state = run(
      [
        { type: "lanesReceived", source: "socket", lanes: [makeLane("T1", 8)] },
        {
          type: "snapshotLoaded",
          snapshot: makeSnapshot({
            lanes: [makeLane("T1", 6), makeLane("T2", 5)],
          }),
        },
      ],
      loaded(),
    );
    expect(state.lanesById.T1.version).toBe(8);
  });

  it("a new lane from a socket event is appended", () => {
    const state = run(
      [
        {
          type: "lanesReceived",
          source: "socket",
          lanes: [makeLane("T9", 1)],
          draftVersion: 12,
        },
      ],
      loaded(),
    );
    expect(state.laneOrder).toEqual(["T1", "T2", "T9"]);
    expect(state.draftVersion).toBe(12);
  });

  it("records the server actor name per lane from socket events", () => {
    const state = run(
      [
        {
          type: "lanesReceived",
          source: "socket",
          lanes: [makeLane("T1", 4)],
          actor: { user_id: "u2", name: "ana" },
          at: 1000,
        },
      ],
      loaded(),
    );
    expect(state.laneActors.T1).toEqual({
      userId: "u2",
      name: "ana",
      at: 1000,
    });
  });
});

describe("pending lanes hold events", () => {
  const started: BoardAction = {
    type: "commandStarted",
    commandId: "c1",
    commandType: "assign_orders",
    truckIds: ["T1"],
  };

  it.each([
    ["socket", "socket"],
    ["fetch", "fetch"],
  ] as const)("a %s event for a pending lane is queued", (_n, source) => {
    const state = run(
      [
        started,
        {
          type: "lanesReceived",
          source,
          lanes: [makeLane("T1", 7), makeLane("T2", 6)],
        },
      ],
      loaded(),
    );
    expect(state.lanesById.T1.version).toBe(3);
    expect(state.queuedLaneEvents.T1.version).toBe(7);
    expect(state.lanesById.T2.version).toBe(6);
    expect(isLanePending(state, "T1")).toBe(true);
  });

  it.each([
    ["queued event newer than answer", 7, 4, 7],
    ["answer newer than queued event", 4, 7, 7],
    ["equal: answer kept", 5, 5, 5],
  ])("on settle, %s", (_n, queuedVersion, answerVersion, expected) => {
    const state = run(
      [
        started,
        {
          type: "lanesReceived",
          source: "socket",
          lanes: [makeLane("T1", queuedVersion)],
        },
        {
          type: "commandSettled",
          commandId: "c1",
          commandType: "assign_orders",
          lanes: [makeLane("T1", answerVersion)],
          undoable: true,
        },
      ],
      loaded(),
    );
    expect(state.lanesById.T1.version).toBe(expected);
    expect(state.queuedLaneEvents.T1).toBeUndefined();
    expect(isLanePending(state, "T1")).toBe(false);
  });

  it("a snapshot during a pending command queues that lane", () => {
    const state = run(
      [
        started,
        {
          type: "snapshotLoaded",
          snapshot: makeSnapshot({
            lanes: [makeLane("T1", 9), makeLane("T2", 5)],
          }),
        },
      ],
      loaded(),
    );
    expect(state.lanesById.T1.version).toBe(3);
    expect(state.queuedLaneEvents.T1.version).toBe(9);
  });

  it("holds until the last pending command on the lane settles", () => {
    const state = run(
      [
        started,
        {
          type: "commandStarted",
          commandId: "c2",
          commandType: "move_stops",
          truckIds: ["T1", "T2"],
        },
        {
          type: "commandSettled",
          commandId: "c1",
          commandType: "assign_orders",
          lanes: [makeLane("T1", 4)],
          undoable: true,
        },
      ],
      loaded(),
    );
    expect(state.lanesById.T1.version).toBe(3);
    expect(state.queuedLaneEvents.T1.version).toBe(4);
    const after = boardReducer(state, {
      type: "commandSettled",
      commandId: "c2",
      commandType: "move_stops",
      lanes: [makeLane("T1", 5), makeLane("T2", 6)],
      undoable: true,
    });
    expect(after.lanesById.T1.version).toBe(5);
    expect(after.lanesById.T2.version).toBe(6);
  });
});

describe("refusal restores origin", () => {
  const origin = loaded([
    makeLane("T1", 3, { loads: [makeLoad("L1", ["O1"])] }),
    makeLane("T2", 5),
  ]);
  const started: BoardAction = {
    type: "commandStarted",
    commandId: "c1",
    commandType: "move_stops",
    truckIds: ["T1", "T2"],
  };

  it("a blocked or network refusal leaves lanes as they were", () => {
    const state = run(
      [started, { type: "commandFailed", commandId: "c1" }],
      origin,
    );
    expect(state.lanesById).toEqual(origin.lanesById);
    expect(state.pending).toEqual({});
    expect(laneOfOrder(state, "O1")?.truck_id).toBe("T1");
  });

  it("a conflict applies the returned server lanes", () => {
    const state = run(
      [
        started,
        { type: "commandFailed", commandId: "c1", lanes: [makeLane("T2", 8)] },
      ],
      origin,
    );
    expect(state.lanesById.T2.version).toBe(8);
    expect(laneOfOrder(state, "O1")?.truck_id).toBe("T1");
  });

  it("releases events queued while it was pending", () => {
    const state = run(
      [
        started,
        { type: "lanesReceived", source: "socket", lanes: [makeLane("T1", 6)] },
        { type: "commandFailed", commandId: "c1" },
      ],
      origin,
    );
    expect(state.lanesById.T1.version).toBe(6);
    expect(state.queuedLaneEvents).toEqual({});
  });

  it("an unknown command id changes nothing", () => {
    expect(
      boardReducer(origin, { type: "commandFailed", commandId: "nope" }),
    ).toBe(origin);
    expect(
      boardReducer(origin, {
        type: "commandSettled",
        commandId: "nope",
        commandType: "assign_orders",
        lanes: [makeLane("T1", 99)],
        undoable: true,
      }),
    ).toBe(origin);
  });
});

describe("undo and redo stacks", () => {
  const settle = (
    id: string,
    type: string,
    undoable: boolean,
    target?: string,
  ): BoardAction[] => [
    {
      type: "commandStarted",
      commandId: id,
      commandType: type,
      truckIds: ["T1"],
    },
    {
      type: "commandSettled",
      commandId: id,
      commandType: type,
      lanes: [],
      undoable,
      targetCommandId: target,
    },
  ];

  it("revert moves the target to redo; reapply moves it back; a new command clears redo", () => {
    let state = run(
      [
        ...settle("a", "assign_orders", true),
        ...settle("b", "move_stops", true),
      ],
      loaded(),
    );
    expect(state.undoStack.map((e) => e.commandId)).toEqual(["a", "b"]);
    state = run(settle("r1", "revert", false, "b"), state);
    expect(state.undoStack.map((e) => e.commandId)).toEqual(["a"]);
    expect(state.redoStack.map((e) => e.commandId)).toEqual(["b"]);
    state = run(settle("r2", "reapply", false, "b"), state);
    expect(state.undoStack.map((e) => e.commandId)).toEqual(["a", "b"]);
    expect(state.redoStack).toEqual([]);
    state = run(
      [
        ...settle("r3", "revert", false, "b"),
        ...settle("c", "pair_driver", true),
      ],
      state,
    );
    expect(state.redoStack).toEqual([]);
    expect(state.undoStack.map((e) => e.commandId)).toEqual(["a", "c"]);
  });

  it(`keeps at most ${UNDO_LIMIT} entries`, () => {
    const actions = Array.from({ length: UNDO_LIMIT + 5 }, (_, i) =>
      settle(`c${i}`, "assign_orders", true),
    ).flat();
    const state = run(actions, loaded());
    expect(state.undoStack).toHaveLength(UNDO_LIMIT);
    expect(state.undoStack[0].commandId).toBe("c5");
  });

  it("publishing a lane drops entries that touch it", () => {
    let state = run(settle("a", "assign_orders", true), loaded());
    state = run(
      [
        {
          type: "commandStarted",
          commandId: "b",
          commandType: "assign_orders",
          truckIds: ["T2"],
        },
        {
          type: "commandSettled",
          commandId: "b",
          commandType: "assign_orders",
          lanes: [],
          undoable: true,
        },
        { type: "lanesPublished", truckIds: ["T1"] },
      ],
      state,
    );
    expect(state.undoStack.map((e) => e.commandId)).toEqual(["b"]);
  });
});

describe("snapshots and selectors", () => {
  it("a full snapshot drops lanes it no longer has, unless pending", () => {
    const state = run(
      [
        {
          type: "commandStarted",
          commandId: "c1",
          commandType: "pair_driver",
          truckIds: ["T2"],
        },
        { type: "snapshotLoaded", snapshot: makeSnapshot({ lanes: [] }) },
      ],
      loaded(),
    );
    expect(Object.keys(state.lanesById)).toEqual(["T2"]);
  });

  it("another day resets pending, stacks and selection", () => {
    const state = run(
      [
        {
          type: "commandStarted",
          commandId: "c1",
          commandType: "pair_driver",
          truckIds: ["T1"],
        },
        { type: "selectionChanged", keys: ["order:O1"] },
        {
          type: "snapshotLoaded",
          snapshot: makeSnapshot({
            service_date: "2026-10-09",
            lanes: [makeLane("T1", 1)],
          }),
        },
      ],
      loaded(),
    );
    expect(state.pending).toEqual({});
    expect(state.selection).toEqual([]);
    expect(state.lanesById.T1.version).toBe(1);
  });

  it("remove_lane settles by deleting a lane missing from the answer", () => {
    const state = run(
      [
        {
          type: "commandStarted",
          commandId: "c1",
          commandType: "remove_lane",
          truckIds: ["T2"],
        },
        {
          type: "commandSettled",
          commandId: "c1",
          commandType: "remove_lane",
          lanes: [],
          undoable: true,
        },
      ],
      loaded(),
    );
    expect(lanesInOrder(state).map((l) => l.truck_id)).toEqual(["T1"]);
  });

  it("finds lanes by order, load and driver", () => {
    const state = loaded([
      makeLane("T1", 1, { loads: [makeLoad("L1", ["O1"])], shelf: ["O9"] }),
      makeLane("T2", 1, { driver_id: "D1" }),
    ]);
    expect(laneOfOrder(state, "O1")?.truck_id).toBe("T1");
    expect(laneOfOrder(state, "O9")?.truck_id).toBe("T1");
    expect(laneOfLoad(state, "L1")?.truck_id).toBe("T1");
    expect(laneOfDriver(state, "D1")?.truck_id).toBe("T2");
  });

  it("selection, place mode and candidates", () => {
    const state = run(
      [
        { type: "selectionChanged", keys: ["order:O1"] },
        {
          type: "placeModeEntered",
          target: { item: { kind: "order", ids: ["O1"] } },
        },
        {
          type: "candidatesReceived",
          results: {
            T1: {
              outcome: "warn",
              worst_checks: [],
              reason: null,
              preview: {
                fill_by_compartment: {},
                insertion_index: 0,
                load_id: null,
                eta_delta_minutes: null,
              },
            },
          },
        },
      ],
      loaded(),
    );
    expect(state.selection).toEqual(["order:O1"]);
    expect(state.placeMode?.item.ids).toEqual(["O1"]);
    expect(state.candidateResults.T1.outcome).toBe("warn");
    const cleared = run(
      [{ type: "placeModeExited" }, { type: "candidatesCleared" }],
      state,
    );
    expect(cleared.placeMode).toBeNull();
    expect(cleared.candidateResults).toEqual({});
  });
});
