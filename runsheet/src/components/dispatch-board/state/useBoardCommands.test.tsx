/**
 * `useBoardCommands` with the real reducer and a mocked `sendBoardCommand`
 * (design K14.2, K4.5, R8.6). Nothing reaches a backend.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { useReducer } from "react";
import { ApiTimeoutError } from "../../../services/api";
import {
  BoardApiError,
  type BoardCommand,
  type CommandResponse,
  type LaneView,
} from "../../../services/dispatchBoardApi";
import { boardReducer, initialBoardState } from "./boardReducer";
import { makeCheck, makeLane, makeLoad, makeSnapshot } from "./testFixtures";
import {
  type CommandOutcome,
  isAlreadyApplied,
  touchedLanes,
  useBoardCommands,
} from "./useBoardCommands";

type Send = jest.Mock<Promise<CommandResponse>, [string, BoardCommand]>;

function ok(
  lanes: LaneView[],
  extra: Partial<CommandResponse> = {},
): CommandResponse {
  return {
    draft_version: 10,
    lanes,
    checks: {},
    already_applied: false,
    audit_degraded: false,
    ...extra,
  };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const startLanes = () => [
  makeLane("T1", 3, { loads: [makeLoad("L1", ["O1"])] }),
  makeLane("T2", 5, { driver_id: "D1" }),
];

function setup(send: Send, onOutcome?: (o: CommandOutcome) => void) {
  return renderHook(() => {
    const [state, dispatch] = useReducer(boardReducer, initialBoardState, (s) =>
      boardReducer(s, {
        type: "snapshotLoaded",
        snapshot: makeSnapshot({ lanes: startLanes() }),
      }),
    );
    const commands = useBoardCommands({
      serviceDate: "2026-10-08",
      state,
      dispatch,
      send,
      onOutcome,
    });
    return { state, commands };
  });
}

describe("touchedLanes", () => {
  const state = {
    ...initialBoardState,
    lanesById: Object.fromEntries(startLanes().map((l) => [l.truck_id, l])),
  };
  it.each([
    [
      { type: "assign_orders", order_ids: ["O9"], truck_id: "T2", target: {} },
      ["T2"],
    ],
    [
      { type: "move_stops", order_ids: ["O1"], truck_id: "T2", target: {} },
      ["T1", "T2"],
    ],
    [{ type: "unassign_orders", order_ids: ["O1"] }, ["T1"]],
    [{ type: "pair_driver", truck_id: "T1", driver_id: "D1" }, ["T1", "T2"]],
    [
      { type: "move_load", load_id: "L1", truck_id: "T2", index: 0 },
      ["T1", "T2"],
    ],
    [{ type: "set_terminal", load_id: "L1", terminal_id: "TERM" }, ["T1"]],
    [{ type: "add_lane", truck_id: "T3" }, ["T3"]],
  ])("%p", (intent, expected) => {
    expect(touchedLanes(state, intent as never)).toEqual(expected);
  });
});

describe("isAlreadyApplied", () => {
  const meta = {
    client_command_id: "x",
    expected_lane_versions: {},
    input_modality: "menu" as const,
  };
  it.each([
    [
      "assign landed",
      { type: "assign_orders", order_ids: ["O1"], truck_id: "T1", target: {} },
      [makeLane("T1", 4, { loads: [makeLoad("L1", ["O1"])] })],
      [],
      true,
    ],
    [
      "assign not there",
      { type: "assign_orders", order_ids: ["O2"], truck_id: "T1", target: {} },
      [makeLane("T1", 4, { loads: [makeLoad("L1", ["O1"])] })],
      [],
      false,
    ],
    [
      "assign wrong load",
      {
        type: "assign_orders",
        order_ids: ["O1"],
        truck_id: "T1",
        target: { load_id: "L2" },
      },
      [makeLane("T1", 4, { loads: [makeLoad("L1", ["O1"])] })],
      [],
      false,
    ],
    [
      "pair landed",
      { type: "pair_driver", truck_id: "T1", driver_id: "D1" },
      [makeLane("T1", 4, { driver_id: "D1" })],
      [],
      true,
    ],
    [
      "pair other driver",
      { type: "pair_driver", truck_id: "T1", driver_id: "D1" },
      [makeLane("T1", 4, { driver_id: "D2" })],
      [],
      false,
    ],
    [
      "remove landed",
      { type: "remove_lane", truck_id: "T1" },
      [],
      ["T1"],
      true,
    ],
    [
      "add landed",
      { type: "add_lane", truck_id: "T1" },
      [makeLane("T1", 1)],
      [],
      true,
    ],
    [
      "unassign landed",
      { type: "unassign_orders", order_ids: ["O1"] },
      [makeLane("T1", 4)],
      [],
      true,
    ],
    [
      "revert never inferred",
      { type: "revert", target_command_id: "y" },
      [makeLane("T1", 4)],
      [],
      false,
    ],
  ])("%s", (_n, c, lanes, missing, expected) => {
    expect(
      isAlreadyApplied(
        { ...meta, ...c } as BoardCommand,
        lanes as LaneView[],
        missing as string[],
      ),
    ).toBe(expected);
  });
});

describe("useBoardCommands", () => {
  it("shows the optimistic entry while pending, then settles server lanes", async () => {
    const d = deferred<CommandResponse>();
    const send: Send = jest.fn(() => d.promise);
    const { result } = setup(send);
    act(() => {
      void result.current.commands.assign(["O9"], "T2", {}, "drag");
    });
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    expect(result.current.commands.optimistic).toEqual([
      expect.objectContaining({
        type: "assign_orders",
        toTruckId: "T2",
        item: { kind: "order", ids: ["O9"] },
      }),
    ]);
    const [date, command] = send.mock.calls[0];
    expect(date).toBe("2026-10-08");
    expect(command).toMatchObject({
      type: "assign_orders",
      order_ids: ["O9"],
      truck_id: "T2",
      expected_lane_versions: { T2: 5 },
      input_modality: "drag",
    });
    expect(command.client_command_id).toMatch(/^[0-9a-f-]{36}$/);
    await act(async () => {
      d.resolve(ok([makeLane("T2", 6, { loads: [makeLoad("L9", ["O9"])] })]));
    });
    await waitFor(() => expect(result.current.commands.optimistic).toEqual([]));
    expect(result.current.state.lanesById.T2.version).toBe(6);
    expect(result.current.state.undoStack.map((e) => e.commandId)).toEqual([
      command.client_command_id,
    ]);
  });

  it("already_applied settles as success", async () => {
    const send: Send = jest
      .fn()
      .mockResolvedValue(
        ok([makeLane("T1", 4, { driver_id: "D9" })], { already_applied: true }),
      );
    const outcomes: CommandOutcome[] = [];
    const { result } = setup(send, (o) => outcomes.push(o));
    await act(async () => {
      await result.current.commands.pair("T1", "D9");
    });
    expect(outcomes[0]).toMatchObject({
      kind: "committed",
      alreadyApplied: true,
    });
    expect(result.current.state.lanesById.T1.driver_id).toBe("D9");
  });

  it("a conflict whose lanes already hold the result settles as success", async () => {
    const lanes = [makeLane("T1", 7, { driver_id: "D9" })];
    const send: Send = jest.fn().mockRejectedValue(
      new BoardApiError("conflict", 409, "BOARD_LANE_CONFLICT", {
        reason: "version_changed",
        lanes,
        missing_lanes: [],
      }),
    );
    const outcomes: CommandOutcome[] = [];
    const { result } = setup(send, (o) => outcomes.push(o));
    await act(async () => {
      await result.current.commands.pair("T1", "D9");
    });
    expect(outcomes[0]).toMatchObject({
      kind: "committed",
      alreadyApplied: true,
    });
    expect(result.current.state.lanesById.T1.version).toBe(7);
    expect(result.current.state.pending).toEqual({});
  });

  it("a real conflict refuses, applies the server lanes and keeps the item at origin", async () => {
    const lanes = [
      makeLane("T1", 9, { loads: [makeLoad("L1", ["O1"])] }),
      makeLane("T2", 6),
    ];
    const send: Send = jest.fn().mockRejectedValue(
      new BoardApiError("conflict", 409, "BOARD_LANE_CONFLICT", {
        reason: "version_changed",
        lanes,
        missing_lanes: [],
      }),
    );
    const outcomes: CommandOutcome[] = [];
    const { result } = setup(send, (o) => outcomes.push(o));
    await act(async () => {
      await result.current.commands.move(["O1"], "T2");
    });
    expect(outcomes[0]).toMatchObject({
      kind: "conflict",
      reason: "version_changed",
    });
    expect(result.current.state.lanesById.T1.version).toBe(9);
    expect(result.current.state.lanesById.T2.loads).toEqual([]);
    expect(result.current.state.undoStack).toEqual([]);
    expect(result.current.commands.optimistic).toEqual([]);
  });

  it("a block refuses with the checks", async () => {
    const checks = {
      T2: [makeCheck({ outcome: "block", reason_code: "cdl_expired" })],
    };
    const send: Send = jest.fn().mockRejectedValue(
      new BoardApiError("blocked", 422, "BOARD_COMMAND_BLOCKED", {
        reason: "cdl_expired",
        checks,
      }),
    );
    const outcomes: CommandOutcome[] = [];
    const { result } = setup(send, (o) => outcomes.push(o));
    await act(async () => {
      await result.current.commands.assign(["O9"], "T2");
    });
    expect(outcomes[0]).toMatchObject({
      kind: "blocked",
      reason: "cdl_expired",
    });
    expect((outcomes[0] as { checks: unknown[] }).checks).toHaveLength(1);
    expect(result.current.state.lanesById.T2.version).toBe(5);
  });

  it("a network failure keeps the command and retry reuses the same id and body", async () => {
    const send: Send = jest
      .fn()
      .mockRejectedValueOnce(new ApiTimeoutError("timed out"))
      .mockResolvedValueOnce(ok([makeLane("T2", 6)]));
    const outcomes: CommandOutcome[] = [];
    const { result } = setup(send, (o) => outcomes.push(o));
    await act(async () => {
      await result.current.commands.assign(["O9"], "T2");
    });
    expect(outcomes[0].kind).toBe("network");
    const first = send.mock.calls[0][1];
    expect(result.current.commands.hasFailed(first.client_command_id)).toBe(
      true,
    );
    await act(async () => {
      await result.current.commands.retry(first.client_command_id);
    });
    expect(send.mock.calls[1][1]).toEqual(first);
    expect(outcomes[1].kind).toBe("committed");
    expect(result.current.commands.hasFailed(first.client_command_id)).toBe(
      false,
    );
    expect(result.current.commands.retry("unknown")).toBeNull();
  });

  it("serializes commands so the second carries the version the first produced", async () => {
    const d = deferred<CommandResponse>();
    const send: Send = jest
      .fn()
      .mockImplementationOnce(() => d.promise)
      .mockResolvedValueOnce(ok([makeLane("T2", 7)]));
    const { result } = setup(send);
    act(() => {
      void result.current.commands.pair("T2", null);
      void result.current.commands.assign(["O9"], "T2");
    });
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    await act(async () => {
      d.resolve(ok([makeLane("T2", 6)]));
    });
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(send.mock.calls[0][1].expected_lane_versions).toEqual({ T2: 5 });
    expect(send.mock.calls[1][1].expected_lane_versions).toEqual({ T2: 6 });
  });

  it("undo sends revert for the top entry and moves it to redo; refusal is undo_stale", async () => {
    const send: Send = jest
      .fn()
      .mockResolvedValueOnce(ok([makeLane("T2", 6)]))
      .mockResolvedValueOnce(ok([makeLane("T2", 7)]))
      .mockRejectedValueOnce(
        new BoardApiError("stale", 409, "BOARD_UNDO_STALE", {
          reason: "changed_by_other",
        }),
      );
    const outcomes: CommandOutcome[] = [];
    const { result } = setup(send, (o) => outcomes.push(o));
    await act(async () => {
      await result.current.commands.pair("T2", null);
    });
    const id = send.mock.calls[0][1].client_command_id;
    await act(async () => {
      await result.current.commands.undo();
    });
    expect(send.mock.calls[1][1]).toMatchObject({
      type: "revert",
      target_command_id: id,
      expected_lane_versions: { T2: 6 },
    });
    expect(result.current.state.undoStack).toEqual([]);
    expect(result.current.state.redoStack.map((e) => e.commandId)).toEqual([
      id,
    ]);
    await act(async () => {
      await result.current.commands.redo();
    });
    expect(outcomes[2]).toMatchObject({
      kind: "undo_stale",
      reason: "changed_by_other",
    });
    expect(result.current.state.redoStack.map((e) => e.commandId)).toEqual([
      id,
    ]);
  });

  it("acknowledge sends no lane versions", async () => {
    const send: Send = jest.fn().mockResolvedValue(ok([makeLane("T1", 3)]));
    const { result } = setup(send);
    await act(async () => {
      await result.current.commands.acknowledge(
        "T1",
        "0123456789abcdef",
        "Customer agreed",
      );
    });
    expect(send.mock.calls[0][1]).toMatchObject({
      type: "acknowledge_warning",
      expected_lane_versions: {},
    });
    expect(result.current.state.undoStack).toEqual([]);
  });

  it("menu, place and keyboard send the same payload apart from input_modality", async () => {
    const send: Send = jest.fn().mockResolvedValue(ok([makeLane("T2", 5)]));
    const { result } = setup(send);
    for (const m of ["drag", "menu", "place", "keyboard"] as const) {
      await act(async () => {
        await result.current.commands.assign(
          ["O9"],
          "T2",
          { load_id: "new" },
          m,
        );
      });
    }
    const strip = (c: BoardCommand) => {
      const { client_command_id: _a, input_modality: _b, ...rest } = c;
      return rest;
    };
    const bodies = send.mock.calls.map((c) => strip(c[1]));
    for (const b of bodies) expect(b).toEqual(bodies[0]);
    expect(send.mock.calls.map((c) => c[1].input_modality)).toEqual([
      "drag",
      "menu",
      "place",
      "keyboard",
    ]);
  });
});
