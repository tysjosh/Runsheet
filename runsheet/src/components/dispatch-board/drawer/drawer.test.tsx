/**
 * Detail drawer (plan task 33; R5.5, R5.6, R8.7, R9.3, R9.4, R9.6, R10.5,
 * R22.3). The board API, fuel reads, socket and Pragmatic are mocked.
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
  getBoardHistory: jest.fn(),
}));
jest.mock("../../../services/fuelApi", () => ({
  ...jest.requireActual("../../../services/fuelApi"),
  listTerminals: jest.fn(),
  getTerminalWaitSummary: jest.fn(),
  listSupplierContracts: jest.fn(),
  getSourcingRecommendations: jest.fn(),
}));
jest.mock("../../../utils/auth", () => ({
  ...jest.requireActual("../../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));

import { getBoardHistory } from "../../../services/dispatchBoardApi";
import {
  getSourcingRecommendations,
  getTerminalWaitSummary,
  listSupplierContracts,
  listTerminals,
} from "../../../services/fuelApi";
import {
  makeCheck,
  makeDriver,
  makeLane,
  makeLoad,
  makeStop,
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
import { resetMocks } from "../testMocks";

const mockHistory = getBoardHistory as Mocked<typeof getBoardHistory>;
const mockTerminals = listTerminals as Mocked<typeof listTerminals>;
const mockWait = getTerminalWaitSummary as Mocked<
  typeof getTerminalWaitSummary
>;
const mockContracts = listSupplierContracts as Mocked<
  typeof listSupplierContracts
>;
const mockSourcing = getSourcingRecommendations as Mocked<
  typeof getSourcingRecommendations
>;

function lane() {
  return makeLane("T1", 4, {
    driver_id: "D1",
    driver: makeDriver("D1", { name: "Ana" }),
    loads: [
      makeLoad("L1", [], {
        terminal_id: "TA",
        stops: [
          makeStop("O1", { location: { lat: 41.9, lon: -87.6 } }),
          makeStop("O2"),
        ],
        allocations: [
          {
            order_id: "O1",
            compartment_id: "T1-c1",
            product_code: "ULSD",
            liters: 9000,
            capacity_liters: 10000,
          },
        ],
        allocation_overrides: {
          O2: [{ compartment_id: "T1-c1", liters: 1000 }],
        },
      }),
    ],
    checks: [
      makeCheck({
        check: "hos",
        outcome: "block",
        reason_code: "hos_blocked",
        message: "Hours of service gate is blocked",
        warning_id: null,
        fix_link: { kind: "hos_override", id: "D1" },
      }),
      makeCheck({
        check: "driver_qualification",
        outcome: "block",
        reason_code: "missing_endorsement",
        message: "Driver lacks the tanker endorsement",
        warning_id: null,
        fix_link: { kind: "driver", id: "D1" },
      }),
      makeCheck({
        check: "order_state",
        outcome: "block",
        reason_code: "order_identity_changed",
        message: "Order changed product",
        scope: { truck_id: "T1", order_id: "O2" },
        warning_id: null,
      }),
      makeCheck({
        scope: { truck_id: "T1", order_id: "O1" },
        warning_id: "w1",
      }),
      makeCheck({
        check: "compartment_fit",
        outcome: "info",
        reason_code: "fill_percent",
        message: "Compartment 1 is 90% full",
        warning_id: null,
      }),
    ],
    outcome: "block",
  });
}

async function openDrawer(snapLane = lane()) {
  await renderBoard(boardSnap({ lanes: [snapLane] }));
  fireEvent.click(screen.getByRole("button", { name: "Truck T1 actions" }));
  fireEvent.click(
    within(screen.getByRole("menu")).getByRole("menuitem", {
      name: "Details",
    }),
  );
  return screen.getByRole("complementary", { name: "Truck T1 details" });
}

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
  mockHistory.mockReset();
  mockHistory.mockResolvedValue({ items: [], next_cursor: null });
  mockTerminals.mockReset();
  mockTerminals.mockResolvedValue({
    items: [
      {
        terminal_id: "TA",
        tenant_id: "t",
        name: "Terminal A",
        operator: "op",
        location_lat: 41.8,
        location_lon: -87.7,
        address: "",
        timezone: "America/Chicago",
        branded: false,
        status: "active",
      },
      {
        terminal_id: "TB",
        tenant_id: "t",
        name: "Terminal B",
        operator: "op",
        location_lat: 41.7,
        location_lon: -87.8,
        address: "",
        timezone: "America/Chicago",
        branded: false,
        status: "active",
      },
    ],
    total: 2,
    page: 1,
    page_size: 200,
    has_next: false,
  });
  mockWait.mockReset();
  mockWait.mockResolvedValue({
    terminal_id: "TA",
    tenant_id: "t",
    window_minutes: 120,
    avg_wait_minutes: 25,
    sample_count: 4,
    wait_warning_threshold_minutes: 60,
    wait_warning_exceeded: false,
    window_start: "",
    window_end: "",
    generated_at: "",
    source: "computed",
  });
  mockContracts.mockReset();
  mockContracts.mockResolvedValue({
    items: [
      {
        contract: {
          contract_id: "K1",
          tenant_id: "t",
          supplier_name: "Acme",
          product_code: "ULSD",
          branded_required: false,
          effective_from: "2026-01-01",
          status: "active",
        },
        lift_summary: {
          yyyy_mm: "2026-10",
          gallons_lifted_this_month: 1200,
          minimum_lift_gallons_per_month: 5000,
          below_minimum: true,
        },
      },
    ],
    total: 1,
    page: 1,
    page_size: 20,
    has_next: false,
  });
  mockSourcing.mockReset();
});

describe("Checks tab (R8.7, R9.6, R10.5)", () => {
  it("groups checks by outcome with their fix links", async () => {
    const drawer = await openDrawer();
    expect(
      within(drawer).getByRole("heading", { name: "Truck T1 details" }),
    ).toHaveFocus();
    const blocking = within(drawer).getByRole("region", {
      name: "Blocking (3)",
    });
    expect(
      within(drawer).getByRole("region", { name: "Warnings (1)" }),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByRole("region", { name: "Information (1)" }),
    ).toBeInTheDocument();
    expect(
      within(blocking).getByRole("link", { name: "Request an HOS override" }),
    ).toHaveAttribute("href", "/dashboard/drivers?driver=D1");
    expect(
      within(blocking).getByRole("link", { name: "Open driver profile" }),
    ).toHaveAttribute("href", "/dashboard/drivers?driver=D1");
    expect(
      within(blocking).getByRole("button", { name: "Remove and re-add" }),
    ).toBeInTheDocument();
  });

  it("a stop's details show that stop's checks and the lane's, not other stops'", async () => {
    await renderBoard(boardSnap({ lanes: [lane()] }));
    fireEvent.contextMenu(
      document.querySelector('[data-focus-key="stop:O1"]') as HTMLElement,
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Details" }));
    const drawer = screen.getByRole("complementary", {
      name: "Truck T1 details",
    });
    expect(within(drawer).getByText(/^Order O1 ·/)).toBeInTheDocument();
    expect(
      within(drawer).queryByText("Order changed product"),
    ).not.toBeInTheDocument();
    expect(
      within(drawer).getByText("Hours of service gate is blocked"),
    ).toBeInTheDocument();
  });

  it("acknowledging a warning needs a 3–500 character reason (R9.3)", async () => {
    mockSend.mockResolvedValue(okResponse([lane()]));
    const drawer = await openDrawer();
    fireEvent.click(
      within(drawer).getByRole("button", { name: "Acknowledge…" }),
    );
    const field = within(drawer).getByLabelText(
      "Reason for accepting this warning",
    );
    expect(field).toHaveFocus();
    const save = within(drawer).getByRole("button", { name: "Acknowledge" });
    fireEvent.change(field, { target: { value: "  ok  " } });
    expect(save).toBeDisabled();
    fireEvent.change(field, { target: { value: " Customer agreed " } });
    expect(save).toBeEnabled();
    await act(async () => {
      fireEvent.click(save);
    });
    await waitFor(() =>
      expect(sentBodies()).toEqual([
        {
          type: "acknowledge_warning",
          truck_id: "T1",
          warning_id: "w1",
          reason: "Customer agreed",
          expected_lane_versions: {},
        },
      ]),
    );
    await waitFor(() =>
      expect(
        within(drawer).queryByLabelText("Reason for accepting this warning"),
      ).not.toBeInTheDocument(),
    );
  });

  it("an acknowledged warning shows its reason instead of the button", async () => {
    await renderBoard(
      boardSnap({
        lanes: [lane()],
        acknowledged: {
          w1: {
            reason: "Customer agreed",
            actor_user_id: "u1",
            at: "2026-10-08T10:00:00Z",
            truck_id: "T1",
          },
        },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Truck T1 actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Details" }));
    expect(
      screen.getByText("Acknowledged: Customer agreed"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Acknowledge…" }),
    ).not.toBeInTheDocument();
  });

  it("Remove and re-add sends unassign, then assign to the same load (R10.5)", async () => {
    const after = lane();
    after.loads[0].stops = after.loads[0].stops.filter(
      (s) => s.order_id !== "O2",
    );
    mockSend
      .mockResolvedValueOnce(okResponse([{ ...after, version: 5 }]))
      .mockResolvedValueOnce(okResponse([{ ...lane(), version: 6 }]));
    const drawer = await openDrawer();
    await act(async () => {
      fireEvent.click(
        within(drawer).getByRole("button", { name: "Remove and re-add" }),
      );
    });
    await waitFor(() => expect(mockSend.mock.calls).toHaveLength(2));
    expect(sentBodies()).toEqual([
      {
        type: "unassign_orders",
        order_ids: ["O2"],
        expected_lane_versions: { T1: 4 },
      },
      {
        type: "assign_orders",
        order_ids: ["O2"],
        truck_id: "T1",
        target: { load_id: "L1" },
        expected_lane_versions: { T1: 5 },
      },
    ]);
  });

  it("Escape closes the drawer", async () => {
    const drawer = await openDrawer();
    fireEvent.keyDown(within(drawer).getByRole("tab", { name: "Checks" }), {
      key: "Escape",
    });
    expect(
      screen.queryByRole("complementary", { name: "Truck T1 details" }),
    ).not.toBeInTheDocument();
  });
});

describe("Compartments tab (R5.5, R5.6)", () => {
  async function openCompartments() {
    const drawer = await openDrawer();
    fireEvent.click(within(drawer).getByRole("tab", { name: "Compartments" }));
    return drawer;
  }

  it("shows the allocation, terminal wait and contract lift", async () => {
    const drawer = await openCompartments();
    expect(
      within(drawer).getByRole("table", { name: "Compartment allocation" }),
    ).toHaveTextContent("Compartment 1O1ULSD2,378 gal90%");
    expect(
      await within(drawer).findByText("Average wait 25 min over the last 2 h."),
    ).toBeInTheDocument();
    expect(
      await within(drawer).findByText(
        "Acme, ULSD: 1,200 gal lifted this month of 5,000 gal minimum (below minimum)",
      ),
    ).toBeInTheDocument();
    expect(mockWait.mock.calls[0]).toEqual(["TA"]);
    expect(mockContracts.mock.calls[0]).toEqual([
      { preferred_terminal_id: "TA", status: "active" },
    ]);
  });

  it("the terminal picker sends set_terminal", async () => {
    mockSend.mockResolvedValue(okResponse([lane()]));
    const drawer = await openCompartments();
    const picker = within(drawer).getByLabelText("Terminal");
    await within(drawer).findByRole("option", { name: "Terminal B" });
    fireEvent.change(picker, { target: { value: "TB" } });
    await act(async () => {
      fireEvent.click(
        within(drawer).getByRole("button", { name: "Set terminal" }),
      );
    });
    await waitFor(() =>
      expect(sentBodies()).toEqual([
        {
          type: "set_terminal",
          load_id: "L1",
          terminal_id: "TB",
          expected_lane_versions: { T1: 4 },
        },
      ]),
    );
  });

  it("sourcing recommendations rank terminals and Use sets one", async () => {
    mockSend.mockResolvedValue(okResponse([lane()]));
    mockSourcing.mockResolvedValue({
      recommendation_id: "r",
      request_id: "q",
      tenant_id: "t",
      product_code: "ULSD",
      volume_gallons: 6000,
      origin_lat: 41.9,
      origin_lon: -87.6,
      rack_price_fallback: false,
      generated_at: "",
      candidates: [
        {
          terminal_id: "TB",
          price_per_gallon_usd: 2.5,
          branded_flag: false,
          avg_wait_minutes: 12,
          distance_km_from_start: 30,
          score: 0.9,
          reasons: ["Lowest price"],
          wait_warning: false,
        },
      ],
    });
    const drawer = await openCompartments();
    await within(drawer).findByRole("option", { name: "Terminal B" });
    fireEvent.click(
      within(drawer).getByRole("button", { name: "Recommend a terminal" }),
    );
    const list = await within(drawer).findByRole("list", {
      name: "Recommended terminals",
    });
    expect(list).toHaveTextContent(
      "Terminal B · $2.500/gal · wait 12 min · 30 km",
    );
    expect(list).toHaveTextContent("Lowest price");
    expect(mockSourcing.mock.calls[0][0]).toEqual({
      product_code: "ULSD",
      volume_gallons: 6000,
      origin_lat: 41.9,
      origin_lon: -87.6,
      truck_id: "T1",
    });
    await act(async () => {
      fireEvent.click(
        within(list).getByRole("button", { name: "Use Terminal B" }),
      );
    });
    await waitFor(() =>
      expect(sentBodies()[0]).toMatchObject({
        type: "set_terminal",
        terminal_id: "TB",
      }),
    );
  });

  it("the override editor sends litres; Reset clears the override (R5.5)", async () => {
    mockSend.mockResolvedValue(okResponse([lane()]));
    const drawer = await openCompartments();
    fireEvent.click(
      within(drawer).getByRole("button", {
        name: "Edit the compartment split for Order O1",
      }),
    );
    const editor = within(drawer).getByRole("group", {
      name: "Compartment split for Order O1",
    });
    const input = within(editor).getByRole("spinbutton");
    expect(input).toHaveValue(2378);
    fireEvent.change(input, { target: { value: "1000" } });
    await act(async () => {
      fireEvent.click(
        within(editor).getByRole("button", { name: "Save split" }),
      );
    });
    await waitFor(() =>
      expect(sentBodies()[0]).toEqual({
        type: "set_allocation",
        load_id: "L1",
        order_id: "O1",
        shares: [{ compartment_id: "T1-c1", liters: 3785.4 }],
        expected_lane_versions: { T1: 4 },
      }),
    );
    await act(async () => {
      fireEvent.click(
        within(drawer).getByRole("button", {
          name: "Reset Order O2 to the automatic split",
        }),
      );
    });
    await waitFor(() =>
      expect(sentBodies()[1]).toMatchObject({
        type: "set_allocation",
        order_id: "O2",
        shares: null,
      }),
    );
  });
});

describe("History tab (R22.3)", () => {
  it("lists the lane's changes newest first with actor names and reasons", async () => {
    mockHistory
      .mockResolvedValueOnce({
        items: [
          {
            command_id: "c2",
            type: "acknowledge_warning",
            result: "committed",
            actor_user_id: "u2",
            actor_name: "Ana",
            input_modality: "menu",
            created_at: "2026-10-08T15:00:00Z",
            lanes: [],
            checks_summary: { T1: { outcome: "warn" } },
            overrides: [{ warning_id: "w1", reason: "<b>Customer</b> agreed" }],
          },
          {
            command_id: "c1",
            type: "assign_orders",
            result: "committed",
            actor_user_id: "u9",
            actor_name: null,
            input_modality: "drag",
            created_at: "2026-10-08T14:00:00Z",
            lanes: [],
            checks_summary: {},
            overrides: [],
          },
        ],
        next_cursor: "cur-1",
      })
      .mockResolvedValueOnce({
        items: [
          {
            command_id: "c0",
            type: "add_lane",
            result: "committed",
            actor_user_id: "u2",
            actor_name: "Ana",
            input_modality: "menu",
            created_at: "2026-10-08T13:00:00Z",
            lanes: [],
            checks_summary: {},
            overrides: [],
          },
        ],
        next_cursor: null,
      });
    const drawer = await openDrawer();
    fireEvent.click(within(drawer).getByRole("tab", { name: "History" }));
    const list = await within(drawer).findByRole("list", {
      name: "Changes, newest first",
    });
    const items = within(list).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent(
      "Ana acknowledged a warning, with warnings",
    );
    expect(items[0]).toHaveTextContent("Reason: <b>Customer</b> agreed");
    expect(items[1]).toHaveTextContent("Another dispatcher assigned orders");
    expect(mockHistory.mock.calls[0]).toEqual([
      TODAY,
      { truck_id: "T1", size: 20 },
    ]);
    fireEvent.click(
      within(drawer).getByRole("button", { name: "Show older changes" }),
    );
    const older = await within(list).findByText(/added the truck/);
    expect(older.closest("li")).toHaveTextContent("Ana added the truck");
    expect(mockHistory.mock.calls[1]).toEqual([
      TODAY,
      { truck_id: "T1", size: 20, cursor: "cur-1" },
    ]);
  });
});
