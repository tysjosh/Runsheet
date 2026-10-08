/**
 * Publish UI (plan task 34; R12.1–R12.3, R12.5, R12.6, R12.8, R10.4, R13.1,
 * R13.7, R13.10). The board API, socket and Pragmatic are mocked.
 */
import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("next/navigation", () => require("../testMocks").navigationMock);
jest.mock(
  "../../../hooks/useDispatchBoardSocket",
  () => require("../testMocks").socketMock,
);
jest.mock(
  "@atlaskit/pragmatic-drag-and-drop/adapter/element-adapter",
  () => require("../testMocks").pragmaticMock,
);
jest.mock(
  "@atlaskit/pragmatic-drag-and-drop-auto-scroll/element",
  () => require("../testMocks").autoScrollMock,
);
jest.mock("../../../hooks/useFleetWebSocket", () => ({
  useFleetWebSocket: jest.fn(),
}));
jest.mock("../../../services/dispatchBoardApi", () => ({
  ...jest.requireActual("../../../services/dispatchBoardApi"),
  getBoard: jest.fn(),
  sendBoardCommand: jest.fn(),
  validateBoard: jest.fn(),
  previewPublish: jest.fn(),
  publishBoard: jest.fn(),
  getPublish: jest.fn(),
}));
jest.mock("../../../utils/auth", () => ({
  ...jest.requireActual("../../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));

import {
  getPublish,
  type LaneView,
  type PublishPreview,
  previewPublish,
  publishBoard,
} from "../../../services/dispatchBoardApi";
import { PUBLISH_POLL_MS } from "../DispatchBoard";
import {
  makeDriver,
  makeLane,
  makeLoad,
  makeStop,
  makeTrayOrder,
} from "../state/testFixtures";
import {
  boardSnap,
  type Mocked,
  mockSend,
  okResponse,
  renderBoard,
  resetBoardMocks,
  sentBodies,
  TODAY,
} from "../testBoard";
import { dropOn, resetMocks, socket, sourceData } from "../testMocks";
import { PREVIEW_DEBOUNCE_MS } from "./PublishDialog";

const mockPreview = previewPublish as Mocked<typeof previewPublish>;
const mockPublish = publishBoard as Mocked<typeof publishBoard>;
const mockGetPublish = getPublish as Mocked<typeof getPublish>;

function lanes(over: Record<string, Partial<LaneView>> = {}): LaneView[] {
  return [
    makeLane("T1", 4, {
      driver_id: "D1",
      driver: makeDriver("D1", { name: "Ana" }),
      loads: [makeLoad("L1", ["O1"])],
      ...over.T1,
    }),
    makeLane("T2", 7, {
      driver_id: "D2",
      driver: makeDriver("D2", { name: "Ben" }),
      state: "modified",
      ever_published: true,
      loads: [
        makeLoad("L2", [], {
          stops: [
            makeStop("O2", {
              snapshot: { ...makeStop("x").snapshot, status: "dispatched" },
            }),
            makeStop("O3"),
          ],
        }),
      ],
      ...over.T2,
    }),
    makeLane("T3", 2, { loads: [makeLoad("L3", ["O4"])], ...over.T3 }),
  ];
}

function preview(over: Partial<PublishPreview> = {}): PublishPreview {
  return {
    ready: true,
    not_ready: [],
    groups: [{ truck_ids: ["T1"], kind: "first_publish", added_lanes: [] }],
    loads: [{ truck_id: "T1", load_id: "L1", class: "new", info: [] }],
    notifications: [],
    open_warnings: [],
    already_published: [],
    ...over,
  };
}

function deferred<T>() {
  let resolve: (v: T) => void = () => {};
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function laneMenu(truckId: string) {
  fireEvent.click(
    screen.getByRole("button", { name: `Truck ${truckId} actions` }),
  );
  return within(screen.getByRole("menu"));
}

async function advance(ms: number) {
  await act(async () => {
    jest.advanceTimersByTime(ms);
  });
}

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
  mockPreview.mockReset();
  mockPreview.mockResolvedValue(preview());
  mockPublish.mockReset();
  mockGetPublish.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
});

describe("PublishDialog dry run (K7.1)", () => {
  it("one call per selection burst, none while typing, aborted responses never shown", async () => {
    await renderBoard(boardSnap({ lanes: lanes() }));
    jest.useFakeTimers();
    const first = deferred<PublishPreview>();
    mockPreview.mockReturnValueOnce(first.promise);
    fireEvent.click(
      laneMenu("T1").getByRole("menuitem", { name: "Publish lane" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Review and publish" });

    await advance(PREVIEW_DEBOUNCE_MS - 1);
    expect(mockPreview.mock.calls).toHaveLength(0);
    await advance(1);
    expect(mockPreview.mock.calls).toHaveLength(1);
    expect(mockPreview.mock.calls[0][1]).toEqual({
      lanes: [{ truck_id: "T1", expected_version: 4 }],
    });
    const firstSignal = mockPreview.mock.calls[0][2] as AbortSignal;

    // A burst of selection changes: one call, after the last one settles.
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /Truck T2/ }));
    await advance(200);
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /Truck T3/ }));
    await advance(200);
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /Truck T3/ }));
    expect(firstSignal.aborted).toBe(true);
    expect(mockPreview.mock.calls).toHaveLength(1);

    const second = deferred<PublishPreview>();
    mockPreview.mockReturnValueOnce(second.promise);
    await advance(PREVIEW_DEBOUNCE_MS);
    expect(mockPreview.mock.calls).toHaveLength(2);
    expect(mockPreview.mock.calls[1][1]).toEqual({
      lanes: [
        { truck_id: "T1", expected_version: 4 },
        { truck_id: "T2", expected_version: 7 },
      ],
    });

    // The aborted call resolves late: never rendered.
    await act(async () => {
      first.resolve(
        preview({
          notifications: [
            {
              driver_id: "DX",
              name: "Stale Driver",
              revoke_order_ids: [],
              assign_order_ids: ["O9"],
              route_updated: false,
            },
          ],
        }),
      );
    });
    expect(within(dialog).queryByText(/Stale Driver/)).not.toBeInTheDocument();
    expect(
      within(dialog).getByText("Checking what will be sent…"),
    ).toBeInTheDocument();

    await act(async () => {
      second.resolve(
        preview({
          groups: [
            { truck_ids: ["T1"], kind: "first_publish", added_lanes: [] },
            { truck_ids: ["T2"], kind: "redispatch", added_lanes: [] },
          ],
          open_warnings: [
            { truck_id: "T2", warning_id: "w2", message: "Window at risk" },
          ],
          not_ready: [{ truck_id: "T2", reasons: ["warning_unacknowledged"] }],
        }),
      );
    });
    const publish = within(dialog).getByRole("button", {
      name: "Publish 2 trucks",
    });
    expect(publish).toBeDisabled();

    // Typing a reason never calls the dry run; a complete reason enables Publish.
    const reason = within(dialog).getByLabelText(
      "Reason for accepting: Window at risk",
    );
    fireEvent.change(reason, { target: { value: "ok" } });
    await advance(PREVIEW_DEBOUNCE_MS * 3);
    expect(publish).toBeDisabled();
    fireEvent.change(reason, { target: { value: "Customer is flexible" } });
    await advance(PREVIEW_DEBOUNCE_MS * 3);
    expect(mockPreview.mock.calls).toHaveLength(2);
    expect(publish).toBeEnabled();
  });
});

describe("PublishDialog content (R12.2, R12.3, R13.7)", () => {
  it("lists drivers, orders, loads, added lanes and what each driver is told", async () => {
    mockPreview.mockResolvedValue(
      preview({
        groups: [
          { truck_ids: ["T2", "T1"], kind: "redispatch", added_lanes: ["T1"] },
        ],
        loads: [
          {
            truck_id: "T2",
            load_id: "L2",
            class: "new_revision",
            info: ["driver_may_be_loading"],
          },
          { truck_id: "T1", load_id: "L1", class: "new", info: [] },
        ],
        notifications: [
          {
            driver_id: "D2",
            name: "Ben",
            revoke_order_ids: ["O2"],
            assign_order_ids: [],
            route_updated: true,
          },
          {
            driver_id: "D1",
            name: "Ana",
            revoke_order_ids: [],
            assign_order_ids: ["O2"],
            route_updated: false,
          },
        ],
      }),
    );
    mockPublish.mockResolvedValue({
      publish_id: "p1",
      lanes: [{ truck_id: "T2", state: "queued" }],
      groups: [],
      already_published: [],
    });
    await renderBoard(boardSnap({ lanes: lanes() }));
    fireEvent.click(
      laneMenu("T2").getByRole("menuitem", { name: "Publish lane" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Review and publish" });
    const t1 = await within(dialog).findByRole("region", { name: "Truck T1" });
    expect(t1).toHaveTextContent(
      "Added to this publish because a dispatched order moved to or from it.",
    );
    const t2 = within(dialog).getByRole("region", { name: "Truck T2" });
    expect(t2).toHaveTextContent("Driver to notifyBen");
    expect(t2).toHaveTextContent("Orders to dispatchO3");
    expect(t2).toHaveTextContent("Load 1: Re-issued to the driver");
    expect(t2).toHaveTextContent(
      "The driver may already be loading at the terminal. Call before re-publishing.",
    );
    const told = within(dialog).getByRole("region", {
      name: "What drivers will be told",
    });
    expect(told).toHaveTextContent("Ben: loses O2, route updated");
    expect(told).toHaveTextContent("Ana: gets O2");

    await act(async () => {
      fireEvent.click(
        within(dialog).getByRole("button", { name: "Publish 2 trucks" }),
      );
    });
    expect(mockPublish.mock.calls[0][0]).toBe(TODAY);
    expect(mockPublish.mock.calls[0][1]).toEqual({
      client_request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
      lanes: [{ truck_id: "T2", expected_version: 7 }],
      warning_reasons: {},
    });
  });

  it("not-ready lanes say why and Publish stays disabled (R12.3)", async () => {
    mockPreview.mockResolvedValue(
      preview({
        ready: false,
        groups: [{ truck_ids: ["T3"], kind: "first_publish", added_lanes: [] }],
        loads: [],
        not_ready: [{ truck_id: "T3", reasons: ["no_driver"] }],
      }),
    );
    await renderBoard(boardSnap({ lanes: lanes() }));
    fireEvent.click(
      laneMenu("T3").getByRole("menuitem", { name: "Publish lane" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Review and publish" });
    expect(
      await within(dialog).findByRole("list", {
        name: "Why Truck T3 isn't ready",
      }),
    ).toHaveTextContent("No driver is paired.");
    expect(dialog).toHaveTextContent(
      "Truck T3 isn't ready. Fix it or remove it from this publish.",
    );
    expect(
      within(dialog).getByRole("button", { name: "Publish" }),
    ).toBeDisabled();
  });

  it("Publish all ready opens the review with the lanes that look ready", async () => {
    await renderBoard(boardSnap({ lanes: lanes() }));
    fireEvent.click(
      screen.getByRole("button", { name: /^Publish (all|\d+) ready$/ }),
    );
    await waitFor(() => expect(mockPreview.mock.calls).toHaveLength(1));
    expect(mockPreview.mock.calls[0][1]).toEqual({
      lanes: [
        { truck_id: "T1", expected_version: 4 },
        { truck_id: "T2", expected_version: 7 },
      ],
    });
  });

  it("a read-only board can't publish", async () => {
    await renderBoard(
      boardSnap({
        lanes: lanes(),
        mode: "shadow",
        read_only: true,
        read_only_reason: "shadow",
      }),
      "shadow",
    );
    expect(
      screen.getByRole("button", { name: /^Publish (all|\d+) ready$/ }),
    ).toBeDisabled();
  });
});

describe("progress, results and toasts (R12.5, R12.8)", () => {
  it("shows socket and polled progress, then announces and toasts the result", async () => {
    mockPublish.mockResolvedValue({
      publish_id: "p1",
      lanes: [{ truck_id: "T1", state: "queued" }],
      groups: [{ truck_ids: ["T1"], kind: "first_publish", added_lanes: [] }],
      already_published: [],
    });
    await renderBoard(boardSnap({ lanes: lanes() }));
    jest.useFakeTimers();
    fireEvent.click(
      laneMenu("T1").getByRole("menuitem", { name: "Publish lane" }),
    );
    await advance(PREVIEW_DEBOUNCE_MS);
    const dialog = screen.getByRole("dialog", { name: "Review and publish" });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: "Publish" }));
    });
    const progress = screen.getByRole("list", { name: "Publish progress" });
    expect(progress).toHaveTextContent("Truck T1Queued");

    await act(async () => {
      socket.handlers.onPublishProgress?.({
        service_date: TODAY,
        publish_id: "p1",
        lanes: [{ truck_id: "T1", state: "publishing", last_result: null }],
      });
    });
    expect(progress).toHaveTextContent("Truck T1Publishing…");

    mockGetPublish.mockResolvedValue({
      publish_id: "p1",
      lanes: [{ truck_id: "T1", state: "published", last_result: null }],
      done: true,
    });
    await advance(PUBLISH_POLL_MS);
    expect(mockGetPublish.mock.calls[0]).toEqual([TODAY, "p1"]);
    expect(
      screen.getByRole("dialog", { name: "Publish finished" }),
    ).toBeInTheDocument();
    expect(progress).toHaveTextContent("Truck T1Published");
    expect(screen.getByTestId("board-live-polite")).toHaveTextContent(
      "Truck T1 published.",
    );
    expect(screen.getAllByText("Truck T1 published.").length).toBeGreaterThan(
      1,
    );

    // Done: no more polling.
    await advance(PUBLISH_POLL_MS * 3);
    expect(mockGetPublish.mock.calls).toHaveLength(1);
  });

  it("a stale review refreshes instead of publishing (version moved)", async () => {
    const { BoardApiError } = jest.requireActual(
      "../../../services/dispatchBoardApi",
    );
    mockPublish.mockRejectedValue(
      new BoardApiError("Not ready", 409, "BOARD_PUBLISH_NOT_READY", {}),
    );
    await renderBoard(boardSnap({ lanes: lanes() }));
    fireEvent.click(
      laneMenu("T1").getByRole("menuitem", { name: "Publish lane" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Review and publish" });
    const publish = await within(dialog).findByRole("button", {
      name: "Publish",
    });
    await waitFor(() => expect(publish).toBeEnabled());
    await act(async () => {
      fireEvent.click(publish);
    });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Something changed since this review.",
    );
    await waitFor(() => expect(mockPreview.mock.calls).toHaveLength(2));
  });
});

describe("failed, recovering and discard (R12.6, R13.10, R10.4)", () => {
  it("a failed lane says what happened; Retry reviews it again", async () => {
    await renderBoard(
      boardSnap({
        lanes: lanes({
          T1: {
            state: "failed",
            publish: {
              ...makeLane("x").publish,
              state: "failed",
              last_result: {
                ...emptyResult(),
                state: "failed",
                stage: "apply",
                writes_made: false,
              },
            },
          },
        }),
      }),
    );
    const text = screen.getByText(
      "Publish failed on Truck T1 while assigning orders. Nothing was changed.",
    );
    const row = text.closest("[role=status]") as HTMLElement;
    fireEvent.click(
      within(row).getByRole("button", { name: "Retry publish on Truck T1" }),
    );
    expect(
      screen.getByRole("dialog", { name: "Review and publish" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(mockPreview.mock.calls).toHaveLength(1));
    expect(mockPreview.mock.calls[0][1]).toEqual({
      lanes: [{ truck_id: "T1", expected_version: 4 }],
    });
  });

  it("forward recovery: assertive banner, read-only lanes, Retry resends the whole group", async () => {
    const attempt = {
      attempt_id: "a1",
      group_truck_ids: ["T1", "T2"],
      phase: "apply" as const,
      loads: {},
      relinks: [],
      notify_baseline: {},
      recovery: "forward" as const,
    };
    const recovering = (truck: string): Partial<LaneView> => ({
      state: "recovering",
      ever_published: true,
      publish: {
        ...makeLane(truck).publish,
        state: "failed",
        attempt,
        last_result: {
          ...emptyResult(),
          state: "failed",
          recovery: "forward",
          writes_made: true,
          drivers_without_routes: ["D1", "D2"],
          group_truck_ids: ["T1", "T2"],
        },
      },
    });
    mockPublish.mockResolvedValue({
      publish_id: "p2",
      lanes: [
        { truck_id: "T1", state: "queued" },
        { truck_id: "T2", state: "queued" },
      ],
      groups: [
        { truck_ids: ["T1", "T2"], kind: "redispatch", added_lanes: [] },
      ],
      already_published: [],
    });
    await renderBoard(
      boardSnap({
        lanes: lanes({ T1: recovering("T1"), T2: recovering("T2") }),
        trays: {
          orders: [makeTrayOrder("O9")],
          orders_truncated: false,
          drivers: [],
          trucks: [],
        },
      }),
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(
      "2 drivers can't see their routes until this publish completes (Trucks T1 and T2). Retry to finish it.",
    );

    // Read-only: a drop is not sent and the lane menu's edits are disabled.
    await act(async () => {
      dropOn(
        document.querySelector('[data-focus-key="lane:T1"]') as HTMLElement,
        sourceData(
          document.querySelector('[data-focus-key="order:O9"]') as HTMLElement,
        ),
      );
    });
    expect(mockSend.mock.calls).toHaveLength(0);
    const menu = laneMenu("T1");
    expect(
      menu.getByRole("menuitem", { name: /Unpair driver/ }),
    ).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(menu.getByRole("menuitem", { name: "Retry publish" }));
    await waitFor(() => expect(mockPublish.mock.calls).toHaveLength(1));
    expect(mockPublish.mock.calls[0][1]).toEqual({
      client_request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
      lanes: [
        { truck_id: "T1", expected_version: 4 },
        { truck_id: "T2", expected_version: 7 },
      ],
    });
    expect(
      screen.queryByRole("dialog", { name: "Review and publish" }),
    ).not.toBeInTheDocument();
  });

  it("a rolled-back group says the previous route is still with the driver", async () => {
    const rolledBack = (truck: string): Partial<LaneView> => ({
      state: "failed",
      ever_published: true,
      publish: {
        ...makeLane(truck).publish,
        state: "failed",
        last_result: {
          ...emptyResult(),
          state: "failed",
          rolled_back: true,
          writes_made: false,
          group_truck_ids: ["T1", "T2"],
        },
      },
    });
    await renderBoard(
      boardSnap({
        lanes: lanes({ T1: rolledBack("T1"), T2: rolledBack("T2") }),
      }),
    );
    expect(
      screen.getByText(
        "Publish rolled back on Trucks T1 and T2. Your previous route is still with the driver. You can edit and retry.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("Discard changes on a Modified lane sends discard_lane_changes", async () => {
    mockSend.mockResolvedValue(okResponse([lanes()[1]]));
    await renderBoard(boardSnap({ lanes: lanes() }));
    // Only a Modified lane offers it.
    expect(
      laneMenu("T1").queryByRole("menuitem", { name: "Discard changes" }),
    ).not.toBeInTheDocument();
    const discard = laneMenu("T2").getByRole("menuitem", {
      name: "Discard changes",
    });
    await act(async () => {
      fireEvent.click(discard);
    });
    await waitFor(() =>
      expect(sentBodies()).toEqual([
        {
          type: "discard_lane_changes",
          truck_id: "T2",
          expected_lane_versions: { T2: 7 },
        },
      ]),
    );
  });
});

function emptyResult() {
  return {
    state: null,
    stage: null,
    reason: null,
    writes_made: null,
    retryable: null,
    failures: [],
    rolled_back: null,
    recovery: null,
    dropped_orders: [],
    notifications_failed: [],
    order_id: null,
    observed_status: null,
    kept_completed_stops: [],
    drivers_without_routes: [],
    group_truck_ids: [],
    publish_id: null,
    at: null,
  };
}
