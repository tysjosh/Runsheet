/**
 * Unit tests for the Dispatch Board client (`dispatchBoardApi.ts`, design K11).
 *
 * `global.fetch` is mocked: these tests cover URL assembly, bodies, envelope
 * unwrapping, `AbortSignal` handling and error code / reason extraction. No
 * test reaches a real backend.
 */
jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: { attemptRefreshingSession: jest.fn().mockResolvedValue(true) },
}));

import { ApiError } from "./api";
import { classifyLoadError, MODULE_DISABLED_NAMES } from "./apiErrors";
import {
  BoardApiError,
  boardErrorCode,
  boardErrorReason,
  boardErrorStatus,
  getBoard,
  getBoardHistory,
  getBoardStatus,
  getPublish,
  isAbortError,
  newClientId,
  previewPublish,
  publishBoard,
  rejectSuggestion,
  sendBoardCommand,
  validateBoard,
} from "./dispatchBoardApi";

const BASE = "http://localhost:8080/api/fuel/board";

type FetchMock = jest.Mock<Promise<unknown>, [string, RequestInit]>;

function respond(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

function mockFetch(...responses: ReturnType<typeof respond>[]): FetchMock {
  const fn = jest.fn() as FetchMock;
  for (const r of responses) fn.mockResolvedValueOnce(r);
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

function call(fn: FetchMock, i = 0): { url: string; init: RequestInit } {
  const [url, init] = fn.mock.calls[i];
  return { url, init };
}

afterEach(() => {
  jest.restoreAllMocks();
});

describe("reads", () => {
  it("getBoardStatus unwraps the envelope", async () => {
    const f = mockFetch(
      respond(200, { data: { mode: "shadow" }, request_id: "r" }),
    );
    await expect(getBoardStatus()).resolves.toEqual({ mode: "shadow" });
    expect(call(f).url).toBe(`${BASE}/status`);
    expect(call(f).init.credentials).toBe("include");
  });

  it("getBoard sends lanes and the three tray filters only when given", async () => {
    const f = mockFetch(
      respond(200, { data: { service_date: "2026-10-08" } }),
      respond(200, { data: { service_date: "2026-10-08" } }),
    );
    await getBoard("2026-10-08");
    expect(call(f, 0).url).toBe(`${BASE}/2026-10-08`);
    await getBoard("2026-10-08", {
      lanes: ["T1", "T2"],
      filters: { call_type: "will_call", product: "ULSD", window: "overdue" },
    });
    const url = new URL(call(f, 1).url);
    expect(url.pathname).toBe("/api/fuel/board/2026-10-08");
    expect(url.searchParams.get("lanes")).toBe("T1,T2");
    expect(url.searchParams.get("call_type")).toBe("will_call");
    expect(url.searchParams.get("product")).toBe("ULSD");
    expect(url.searchParams.get("window")).toBe("overdue");
  });

  it("getPublish and getBoardHistory build their paths", async () => {
    const f = mockFetch(
      respond(200, { data: { publish_id: "p1", lanes: [], done: true } }),
      respond(200, { data: { items: [], next_cursor: null } }),
    );
    await getPublish("2026-10-08", "p1");
    await getBoardHistory("2026-10-08", { truck_id: "T1", size: 20 });
    expect(call(f, 0).url).toBe(`${BASE}/2026-10-08/publish/p1`);
    const url = new URL(call(f, 1).url);
    expect(url.pathname).toBe("/api/fuel/board/2026-10-08/history");
    expect(url.searchParams.get("truck_id")).toBe("T1");
    expect(url.searchParams.get("size")).toBe("20");
  });
});

describe("writes", () => {
  it("sendBoardCommand posts the command and returns already_applied", async () => {
    const f = mockFetch(
      respond(200, {
        data: {
          draft_version: 3,
          lanes: [],
          checks: {},
          already_applied: true,
          audit_degraded: false,
        },
      }),
    );
    const cmd = {
      type: "pair_driver" as const,
      truck_id: "T1",
      driver_id: "D1",
      client_command_id: newClientId(),
      expected_lane_versions: { T1: 2 },
      input_modality: "menu" as const,
    };
    const res = await sendBoardCommand("2026-10-08", cmd);
    expect(res.already_applied).toBe(true);
    expect(call(f).url).toBe(`${BASE}/2026-10-08/commands`);
    expect(call(f).init.method).toBe("POST");
    expect(JSON.parse(call(f).init.body as string)).toEqual(cmd);
  });

  it("validateBoard posts item, candidates and position", async () => {
    const f = mockFetch(
      respond(200, { data: { results: {}, degraded_sources: [] } }),
    );
    await validateBoard("2026-10-08", {
      item: { kind: "order", ids: ["O1"] },
      candidates: ["T1"],
      position: { load_id: "L1", index: 2 },
    });
    expect(call(f).url).toBe(`${BASE}/2026-10-08/validate`);
    expect(JSON.parse(call(f).init.body as string)).toEqual({
      item: { kind: "order", ids: ["O1"] },
      candidates: ["T1"],
      position: { load_id: "L1", index: 2 },
    });
  });

  it("publishBoard sends dry_run false and returns groups", async () => {
    const groups = [
      { truck_ids: ["T1", "T7"], kind: "redispatch", added_lanes: ["T7"] },
    ];
    const f = mockFetch(
      respond(202, {
        data: {
          publish_id: "p1",
          lanes: [{ truck_id: "T1", state: "queued" }],
          groups,
          already_published: [],
        },
      }),
    );
    const res = await publishBoard("2026-10-08", {
      client_request_id: "11111111-1111-4111-8111-111111111111",
      lanes: [{ truck_id: "T1", expected_version: 4 }],
    });
    expect(res.groups).toEqual(groups);
    expect(JSON.parse(call(f).init.body as string)).toMatchObject({
      dry_run: false,
    });
  });

  it("previewPublish sends dry_run true and no client_request_id", async () => {
    const preview = {
      ready: false,
      not_ready: [{ truck_id: "T2", reasons: ["no_driver"] }],
      groups: [{ truck_ids: ["T1"], kind: "first_publish", added_lanes: [] }],
      loads: [],
      notifications: [],
      open_warnings: [],
      already_published: [],
    };
    const f = mockFetch(respond(200, { data: preview }));
    const res = await previewPublish("2026-10-08", {
      lanes: [{ truck_id: "T1", expected_version: 4 }],
    });
    expect(res).toEqual(preview);
    const body = JSON.parse(call(f).init.body as string);
    expect(body.dry_run).toBe(true);
    expect(body).not.toHaveProperty("client_request_id");
  });

  it("rejectSuggestion sends a trimmed reason or an empty body", async () => {
    const f = mockFetch(
      respond(200, { data: { plan_id: "P1", dismissed: true } }),
      respond(200, { data: { plan_id: "P1", dismissed: true } }),
    );
    await rejectSuggestion("2026-10-08", "P1", "  wrong truck ");
    await rejectSuggestion("2026-10-08", "P1");
    expect(call(f, 0).url).toBe(`${BASE}/2026-10-08/suggestions/P1/reject`);
    expect(JSON.parse(call(f, 0).init.body as string)).toEqual({
      reason: "wrong truck",
    });
    expect(JSON.parse(call(f, 1).init.body as string)).toEqual({});
  });
});

describe("abort signal", () => {
  it("previewPublish rethrows a caller abort as AbortError", async () => {
    global.fetch = jest.fn(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            const err = new Error("aborted");
            err.name = "AbortError";
            reject(err);
          });
        }),
    ) as unknown as typeof fetch;
    const controller = new AbortController();
    const pending = previewPublish(
      "2026-10-08",
      { lanes: [{ truck_id: "T1", expected_version: 1 }] },
      controller.signal,
    );
    controller.abort();
    const err = await pending.catch((e) => e);
    expect(isAbortError(err)).toBe(true);
    expect(err).not.toBeInstanceOf(ApiError);
  });

  it("an already aborted signal never resolves a response", async () => {
    global.fetch = jest.fn((_url: string, init: RequestInit) => {
      if (init.signal?.aborted) {
        const err = new Error("aborted");
        err.name = "AbortError";
        return Promise.reject(err);
      }
      return Promise.resolve(respond(200, { data: {} }));
    }) as unknown as typeof fetch;
    const controller = new AbortController();
    controller.abort();
    const err = await validateBoard(
      "2026-10-08",
      { item: { kind: "order", ids: ["O1"] }, candidates: ["T1"] },
      controller.signal,
    ).catch((e) => e);
    expect(isAbortError(err)).toBe(true);
  });
});

describe("errors", () => {
  it("exposes code, details and details.reason", async () => {
    const lanes = [{ truck_id: "T1", version: 5 }];
    mockFetch(
      respond(409, {
        error_code: "BOARD_LANE_CONFLICT",
        message: "Another change reached this truck first.",
        details: { reason: "version_changed", lanes, missing_lanes: [] },
        request_id: "r",
      }),
    );
    const err = await sendBoardCommand("2026-10-08", {
      type: "remove_lane",
      truck_id: "T1",
      client_command_id: newClientId(),
      expected_lane_versions: { T1: 4 },
      input_modality: "menu",
    }).catch((e) => e);
    expect(err).toBeInstanceOf(BoardApiError);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.message).toBe("Another change reached this truck first.");
    expect(boardErrorCode(err)).toBe("BOARD_LANE_CONFLICT");
    expect(boardErrorReason(err)).toBe("version_changed");
    expect(err.details.lanes).toEqual(lanes);
  });

  it("tolerates a non-JSON body", async () => {
    global.fetch = jest.fn().mockResolvedValueOnce({
      ok: false,
      status: 502,
      json: async () => {
        throw new SyntaxError("bad json");
      },
    }) as unknown as typeof fetch;
    const err = await getBoardStatus().catch((e) => e);
    expect(err).toBeInstanceOf(BoardApiError);
    expect(err.status).toBe(502);
    expect(boardErrorCode(err)).toBeUndefined();
    expect(boardErrorReason(err)).toBeUndefined();
  });

  it("a network failure is status 0", async () => {
    global.fetch = jest
      .fn()
      .mockRejectedValueOnce(
        new TypeError("Failed to fetch"),
      ) as unknown as typeof fetch;
    const err = await getBoard("2026-10-08").catch((e) => e);
    expect(boardErrorStatus(err)).toBe(0);
  });

  it("a disabled board classifies as module disabled with its name", async () => {
    mockFetch(
      respond(404, {
        error_code: "DISPATCH_BOARD_DISABLED",
        message: "Dispatch Board is not enabled.",
      }),
    );
    const err = await getBoard("2026-10-08").catch((e) => e);
    expect(MODULE_DISABLED_NAMES.DISPATCH_BOARD_DISABLED).toBe(
      "Dispatch Board",
    );
    expect(classifyLoadError(err, "x")).toMatchObject({
      kind: "module_disabled",
      moduleName: "Dispatch Board",
    });
  });
});

describe("newClientId", () => {
  it("returns a uuid4", () => {
    const re =
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
    const a = newClientId();
    expect(a).toMatch(re);
    expect(newClientId()).not.toBe(a);
  });
});
