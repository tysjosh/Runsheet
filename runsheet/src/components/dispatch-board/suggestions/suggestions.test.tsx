/**
 * Suggestions UI (plan task 35; R16.1–R16.4, R16.6). The board API, fuel
 * API, socket and Pragmatic are mocked.
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
  rejectSuggestion: jest.fn(),
}));
jest.mock("../../../services/fuelApi", () => ({
  ...jest.requireActual("../../../services/fuelApi"),
  generatePlan: jest.fn(),
}));
jest.mock("../../../services/tenant", () => ({
  getCurrentTenantId: () => "tenant-x",
}));
jest.mock("../../../utils/auth", () => ({
  ...jest.requireActual("../../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));

import {
  BoardApiError,
  type BoardSuggestion,
  rejectSuggestion,
} from "../../../services/dispatchBoardApi";
import { generatePlan } from "../../../services/fuelApi";
import {
  makeCheck,
  makeDriver,
  makeLane,
  makeLoad,
} from "../state/testFixtures";
import {
  boardSnap,
  type Mocked,
  mockGetBoard,
  mockSend,
  okResponse,
  renderBoard,
  resetBoardMocks,
  sentBodies,
  sentModalities,
  TODAY,
} from "../testBoard";
import { navigation, resetMocks } from "../testMocks";
import { addDays } from "../viewState";

const mockReject = rejectSuggestion as Mocked<typeof rejectSuggestion>;
const mockGenerate = generatePlan as Mocked<typeof generatePlan>;

function suggestion(
  id: string,
  truckId: string,
  loads: { truck: string; orders: string[]; key?: string }[],
): BoardSuggestion {
  return {
    suggestion_id: id,
    plan_id: id,
    truck_id: truckId,
    status: "proposed",
    agent: "compartment_loading",
    agent_name: "Compartment Loading Agent",
    route_agent_name: "Route Planning Agent",
    route_id: `r-${id}`,
    created_at: "2026-10-08T06:00:00Z",
    approval_action_id: null,
    loads: loads.map((l) => ({
      load_key: l.key ?? id,
      truck_id: l.truck,
      terminal_id: null,
      order_ids: l.orders,
    })),
    why: {
      text: "Priority high (90). Tank runs out in 6 h. Fills 80% of the truck.",
      priority_score: 90,
      priority_bucket: "high",
      runout_hours: 6,
      window_end: null,
      fill_pct: 80,
    },
    diff: {
      diff_id: id,
      original_route_id: "",
      patched_route_id: "",
      added_stops: loads[0].orders.map((o, i) => ({
        stop_id: o,
        index: i,
        gallons: 1000,
      })),
      removed_stops: [],
      reordered_stops: [],
      reassigned_stops: [
        { stop_id: "O8", from_truck_id: "T2", to_truck_id: truckId },
      ],
      quantity_changes: [],
      eta_shifts: [],
      generated_at: "2026-10-08T06:00:00Z",
    },
  };
}

function snapshot(acceptedS1 = false) {
  return boardSnap({
    lanes: [
      makeLane("T1", 3, {
        driver_id: "D1",
        driver: makeDriver("D1", { name: "Ana" }),
        loads: acceptedS1
          ? [
              makeLoad("L9", ["O5", "O6"], {
                source: "suggestion",
                suggestion_id: "S1",
              }),
            ]
          : [],
      }),
    ],
    suggestions: [
      suggestion("S1", "T1", [{ truck: "T1", orders: ["O5", "O6"] }]),
      suggestion("S2", "T1", [{ truck: "T1", orders: ["O7"] }]),
      suggestion("S3", "T4", [
        { truck: "T4", orders: ["O10"], key: "S3-T4" },
        { truck: "T5", orders: ["O11"], key: "S3-T5" },
      ]),
    ],
  });
}

const ghost = () =>
  screen.getAllByRole("button", { name: /^Suggested load from/ });

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
  mockReject.mockReset();
  mockGenerate.mockReset();
});

it("shows open suggestions as ghost loads on their lane (R16.1)", async () => {
  await renderBoard(snapshot());
  expect(ghost().map((b) => b.getAttribute("aria-label"))).toEqual([
    "Suggested load from Compartment Loading Agent, 2 stops. Review",
    "Suggested load from Compartment Loading Agent, 1 stop. Review",
  ]);
});

it("an accepted suggestion leaves the lane", async () => {
  await renderBoard(snapshot(true));
  expect(ghost()).toHaveLength(1);
});

it("a ghost load opens the why panel and the diff (R16.2)", async () => {
  await renderBoard(snapshot());
  fireEvent.click(ghost()[0]);
  const dialog = screen.getByRole("dialog", {
    name: "Suggestions for Truck T1",
  });
  const cards = within(dialog).getAllByRole("listitem");
  expect(cards[0]).toHaveTextContent(
    "Priority high (90). Tank runs out in 6 h. Fills 80% of the truck.",
  );
  expect(cards[0]).toHaveTextContent(
    "Compartment Loading Agent and Route Planning Agent",
  );
  // The clicked suggestion opens expanded: priority, runout, fill and the diff.
  expect(cards[0]).toHaveTextContent("Priorityhigh (90)");
  expect(cards[0]).toHaveTextContent("Tank runs out in6 h");
  expect(cards[0]).toHaveTextContent("Truck fill80%");
  const diff = within(cards[0]).getByRole("region", {
    name: "Changes to the board",
  });
  expect(
    within(diff).getByRole("button", { name: /Added stops/ }),
  ).toHaveTextContent("2");
  expect(diff).toHaveTextContent("O5");
  expect(
    within(diff).getByRole("button", { name: /Reassigned stops/ }),
  ).toHaveTextContent("1");
  // The other card is collapsed.
  expect(
    within(cards[1]).getByRole("button", { name: "Why and changes" }),
  ).toHaveAttribute("aria-expanded", "false");
});

it("Accept sends accept_suggestion through the command path (R16.3)", async () => {
  mockSend.mockResolvedValue(okResponse(snapshot(true).lanes));
  await renderBoard(snapshot());
  fireEvent.click(ghost()[0]);
  const dialog = screen.getByRole("dialog", {
    name: "Suggestions for Truck T1",
  });
  await act(async () => {
    fireEvent.click(
      within(dialog).getAllByRole("button", {
        name: "Accept the suggestion for Truck T1",
      })[0],
    );
  });
  await waitFor(() =>
    expect(sentBodies()).toEqual([
      {
        type: "accept_suggestion",
        suggestion_id: "S1",
        load_ids: null,
        expected_lane_versions: { T1: 3 },
      },
    ]),
  );
  expect(sentModalities()).toEqual(["suggestion"]);
});

it("Accept all for a truck accepts each of its suggestions", async () => {
  mockSend.mockResolvedValue(okResponse(snapshot().lanes));
  await renderBoard(snapshot());
  fireEvent.click(ghost()[0]);
  await act(async () => {
    fireEvent.click(
      screen.getByRole("button", { name: "Accept all for Truck T1" }),
    );
  });
  await waitFor(() => expect(mockSend.mock.calls).toHaveLength(2));
  expect(
    sentBodies().map((b) => (b as { suggestion_id: string }).suggestion_id),
  ).toEqual(["S1", "S2"]);
});

it("Accept all marks a blocked suggestion and offers its passing loads (R16.3)", async () => {
  mockSend.mockImplementation(async (_date, cmd) => {
    if (
      cmd.type === "accept_suggestion" &&
      cmd.suggestion_id === "S3" &&
      !cmd.load_ids
    ) {
      throw new BoardApiError("Blocked", 422, "BOARD_COMMAND_BLOCKED", {
        checks: {
          T4: [
            makeCheck({
              outcome: "block",
              scope: { truck_id: "T4" },
              message: "Driver is not qualified",
            }),
          ],
        },
      });
    }
    return okResponse(snapshot().lanes);
  });
  await renderBoard(snapshot());
  fireEvent.click(screen.getByRole("button", { name: "3 suggestions" }));
  const dialog = screen.getByRole("dialog", { name: "Agent suggestions" });
  await act(async () => {
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Accept all (3)" }),
    );
  });
  await waitFor(() => expect(mockSend.mock.calls).toHaveLength(3));
  const t4 = within(dialog).getByRole("region", {
    name: "Truck T4 suggestions",
  });
  expect(t4).toHaveTextContent("Not applied. Driver is not qualified.");
  expect(t4).toHaveTextContent("Accept only the 1 load that passes?");
  await waitFor(() =>
    expect(screen.getByTestId("board-live-polite")).toHaveTextContent(
      "Accepted 2 of 3 suggestions. 1 is blocked and marked.",
    ),
  );
  await act(async () => {
    fireEvent.click(within(t4).getByRole("button", { name: "Accept 1 load" }));
  });
  await waitFor(() => expect(mockSend.mock.calls).toHaveLength(4));
  expect(sentBodies()[3]).toMatchObject({
    type: "accept_suggestion",
    suggestion_id: "S3",
    load_ids: ["S3-T5"],
  });
});

it("Reject sends the optional reason and refreshes (R16.4)", async () => {
  mockReject.mockResolvedValue({ plan_id: "S2", dismissed: true });
  await renderBoard(snapshot());
  fireEvent.click(ghost()[1]);
  const dialog = screen.getByRole("dialog", {
    name: "Suggestions for Truck T1",
  });
  const card = within(dialog).getAllByRole("listitem")[1];
  fireEvent.click(
    within(card).getByRole("button", {
      name: "Reject the suggestion for Truck T1",
    }),
  );
  fireEvent.change(within(card).getByLabelText("Reason (optional)"), {
    target: { value: "Wrong truck" },
  });
  const loads = mockGetBoard.mock.calls.length;
  await act(async () => {
    fireEvent.click(within(card).getByRole("button", { name: "Reject" }));
  });
  expect(mockReject.mock.calls[0]).toEqual([TODAY, "S2", "Wrong truck"]);
  await waitFor(() => expect(mockGetBoard.mock.calls.length).toBe(loads + 1));
  expect(
    screen.getAllByText("Suggestion for Truck T1 rejected.").length,
  ).toBeGreaterThan(0);
});

it("Generate plan calls the existing client and refetches (R16.6)", async () => {
  mockGenerate.mockResolvedValue({ run_id: "run-1", status: "complete" });
  await renderBoard(snapshot());
  const loads = mockGetBoard.mock.calls.length;
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Generate plan" }));
  });
  expect(mockGenerate.mock.calls[0]).toEqual(["tenant-x"]);
  await waitFor(() => expect(mockGetBoard.mock.calls.length).toBe(loads + 1));
  expect(screen.getByTestId("board-live-polite")).toHaveTextContent(
    "Plan generated. Suggestions appear on the trucks as they arrive.",
  );
});

it("Generate plan is only for today", async () => {
  const future = addDays(TODAY, 3);
  navigation.params = new URLSearchParams(`date=${future}`);
  await renderBoard({ ...snapshot(), service_date: future });
  expect(screen.getByRole("button", { name: "Generate plan" })).toBeDisabled();
});
