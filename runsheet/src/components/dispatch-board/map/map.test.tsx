/**
 * Map linkage (plan task 36; R17.1–R17.4, R15.4). `@vis.gl/react-google-maps`
 * and the fleet socket are mocked: markers render as buttons named by their
 * title, and the test pushes live positions through the captured options.
 */
import {
  act,
  fireEvent,
  render,
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
jest.mock("@vis.gl/react-google-maps", () => {
  const React = require("react");
  return {
    APIProvider: ({ children }: { children: unknown }) => children,
    Map: ({ children }: { children: unknown }) =>
      React.createElement("div", { "data-testid": "google-map" }, children),
    AdvancedMarker: ({
      title,
      onClick,
      children,
    }: {
      title: string;
      onClick?: () => void;
      children: unknown;
    }) =>
      React.createElement(
        "button",
        { type: "button", "data-marker": "", "aria-label": title, onClick },
        children,
      ),
    useMap: () => null,
  };
});
const mockFleet: { options: Record<string, any> | null } = { options: null };
jest.mock("../../../hooks/useFleetWebSocket", () => ({
  useFleetWebSocket: (options: Record<string, unknown>) => {
    mockFleet.options = options;
    return { state: "connected", isConnected: true };
  },
}));
jest.mock("../../../services/dispatchBoardApi", () => ({
  ...jest.requireActual("../../../services/dispatchBoardApi"),
  getBoard: jest.fn(),
  sendBoardCommand: jest.fn(),
  validateBoard: jest.fn(),
  getBoardHistory: jest
    .fn()
    .mockResolvedValue({ items: [], next_cursor: null }),
}));
jest.mock("../../../services/fuelApi", () => ({
  ...jest.requireActual("../../../services/fuelApi"),
  listTerminals: jest.fn(),
}));
jest.mock("../../../utils/auth", () => ({
  ...jest.requireActual("../../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));

import type { LaneView } from "../../../services/dispatchBoardApi";
import { listTerminals } from "../../../services/fuelApi";
import {
  makeDriver,
  makeLane,
  makeLoad,
  makeStop,
} from "../state/testFixtures";
import {
  boardSnap,
  type Mocked,
  renderBoard,
  resetBoardMocks,
} from "../testBoard";
import { registry, resetMocks } from "../testMocks";
import { LaneRouteMap, laneColour } from "./LaneRouteMap";
import {
  LATE_THRESHOLD_MINUTES,
  minutesLate,
  POSITION_MAX_AGE_MS,
} from "./runningLate";

const mockTerminals = listTerminals as Mocked<typeof listTerminals>;
const KEY = "NEXT_PUBLIC_GOOGLE_MAPS_API_KEY";
const STOP_A = { lat: 41.9, lon: -87.6 };
const STOP_B = { lat: 42.0, lon: -87.7 };

function published(
  truckId: string,
  orders: [string, typeof STOP_A][],
  eta?: string,
) {
  return makeLane(truckId, 3, {
    driver_id: `D-${truckId}`,
    driver: makeDriver(`D-${truckId}`),
    state: "published",
    ever_published: true,
    loads: [
      makeLoad(`L-${truckId}`, [], {
        terminal_id: "TA",
        stops: orders.map(([id, location], i) =>
          makeStop(id, {
            location,
            eta: i === 0 ? (eta ?? null) : null,
            snapshot: { ...makeStop(id).snapshot, status: "dispatched" },
          }),
        ),
      }),
    ],
    publish: {
      ...makeLane(truckId).publish,
      state: "published",
      plans: {
        [`L-${truckId}`]: {
          plan_id: `bp-L-${truckId}-r1`,
          route_id: `br-L-${truckId}-r1`,
          run_id: `bp-L-${truckId}-r1`,
          revision: 1,
        },
      },
    },
  });
}

const isoIn = (minutes: number) =>
  new Date(Date.now() + minutes * 60_000).toISOString();

function marker(name: string) {
  return screen.getByRole("button", { name });
}

async function openMapTab(lanes: LaneView[]) {
  await renderBoard(boardSnap({ lanes }));
  fireEvent.click(screen.getByRole("button", { name: "Truck T1 actions" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Details" }));
  const drawer = screen.getByRole("complementary", {
    name: "Truck T1 details",
  });
  fireEvent.click(within(drawer).getByRole("tab", { name: "Map" }));
  return drawer;
}

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
  mockFleet.options = null;
  process.env[KEY] = "test-key";
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
    ],
    total: 1,
    page: 1,
    page_size: 200,
    has_next: false,
  });
});

afterAll(() => {
  delete process.env[KEY];
});

describe("drawer map (R17.1, R17.2, R17.4)", () => {
  it("draws the terminal and numbered stops in order", async () => {
    const drawer = await openMapTab([
      published("T1", [
        ["O1", STOP_A],
        ["O2", STOP_B],
      ]),
    ]);
    const map = within(drawer).getByRole("region", {
      name: "Route map for Truck T1",
    });
    expect(
      await within(map).findByRole("button", { name: "Terminal for Truck T1" }),
    ).toBeInTheDocument();
    expect(
      within(map)
        .getAllByRole("button", { name: /^Stop \d, Order/ })
        .filter((b) => b.hasAttribute("data-marker"))
        .map((b) => b.getAttribute("aria-label")),
    ).toEqual(["Stop 1, Order O1, Truck T1", "Stop 2, Order O2, Truck T1"]);
    // Never a drop target (R17.4).
    expect(registry.targets.some((t) => map.contains(t.element))).toBe(false);
  });

  it("selecting a stop on the map selects it on the board, and back (R17.2)", async () => {
    const drawer = await openMapTab([
      published("T1", [
        ["O1", STOP_A],
        ["O2", STOP_B],
      ]),
    ]);
    fireEvent.click(marker("Stop 2, Order O2, Truck T1"));
    const card = document.querySelector(
      '[data-focus-key="stop:O2"]',
    ) as HTMLElement;
    expect(card).toHaveAttribute("aria-selected", "true");
    expect(
      within(drawer).getByRole("button", { name: "Stop 2, Order O2" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      marker("Stop 2, Order O2, Truck T1").querySelector("[data-selected]"),
    ).toHaveAttribute("data-selected", "true");

    // The list under the map selects too (keyboard path).
    fireEvent.click(
      within(drawer).getByRole("button", { name: "Stop 1, Order O1" }),
    );
    expect(
      document.querySelector('[data-focus-key="stop:O1"]'),
    ).toHaveAttribute("aria-selected", "true");
    expect(card).toHaveAttribute("aria-selected", "false");
  });

  it("shows the truck's live position from the fleet feed", async () => {
    await openMapTab([published("T1", [["O1", STOP_A]])]);
    expect(mockFleet.options).not.toBeNull();
    act(() => {
      mockFleet.options?.onLocationUpdate({
        truck_id: "T1",
        coordinates: { lat: 41.95, lon: -87.65 },
        timestamp: new Date().toISOString(),
      });
    });
    expect(marker("Truck T1, live position")).toBeInTheDocument();
  });

  it("without a Maps key the tab shows an empty state", async () => {
    delete process.env[KEY];
    const drawer = await openMapTab([published("T1", [["O1", STOP_A]])]);
    expect(within(drawer).getByText("Map unavailable")).toBeInTheDocument();
    expect(screen.queryByTestId("google-map")).not.toBeInTheDocument();
  });
});

describe("split view (R17.3)", () => {
  it("shows the routes of every lane on screen, one colour each", async () => {
    await renderBoard(
      boardSnap({
        lanes: [
          published("T1", [["O1", STOP_A]]),
          published("T2", [["O3", STOP_B]]),
        ],
      }),
    );
    const toggle = screen.getByRole("button", { name: "Map" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    const region = await screen.findByRole("region", {
      name: "Routes of the trucks on screen",
    });
    await waitFor(() =>
      expect(
        within(region).getByRole("button", {
          name: "Truck T2, Stop 1, Order O3",
        }),
      ).toBeInTheDocument(),
    );
    expect(
      within(region).getByRole("button", {
        name: "Truck T1, Stop 1, Order O1",
      }),
    ).toBeInTheDocument();
  });

  it("lanes past 12 reuse a colour with a dashed line", () => {
    expect(laneColour(0)).toEqual({
      colour: laneColour(12).colour,
      dashed: false,
    });
    expect(laneColour(12).dashed).toBe(true);
  });
});

describe("Running late (R15.4)", () => {
  it("badges a published lane whose truck is past the next stop's ETA", async () => {
    await renderBoard(
      boardSnap({
        lanes: [
          published("T1", [["O1", STOP_A]], isoIn(-30)),
          published("T2", [["O3", STOP_B]], isoIn(-5)),
        ],
      }),
    );
    expect(screen.queryByText(/Running late/)).not.toBeInTheDocument();
    act(() => {
      mockFleet.options?.onBatchLocationUpdate([
        {
          truck_id: "T1",
          coordinates: STOP_A,
          timestamp: new Date().toISOString(),
        },
        {
          truck_id: "T2",
          coordinates: STOP_B,
          timestamp: new Date().toISOString(),
        },
      ]);
    });
    expect(screen.getByText("Running late +30 min")).toBeInTheDocument();
    expect(screen.getAllByText(/Running late/)).toHaveLength(1);
  });

  it("minutesLate: threshold, stale positions, unpublished lanes and finished stops", () => {
    const now = Date.parse("2026-10-08T15:00:00Z");
    const at = (iso: string) => ({ ...STOP_A, at: Date.parse(iso) });
    const lane = published("T1", [["O1", STOP_A]], "2026-10-08T14:44:00Z");
    expect(minutesLate(lane, at("2026-10-08T14:59:00Z"), now)).toBe(16);
    const onTime = published("T1", [["O1", STOP_A]], "2026-10-08T14:45:00Z");
    expect(minutesLate(onTime, at("2026-10-08T14:59:00Z"), now)).toBeNull();
    expect(LATE_THRESHOLD_MINUTES).toBe(15);
    expect(
      minutesLate(lane, { ...STOP_A, at: now - POSITION_MAX_AGE_MS - 1 }, now),
    ).toBeNull();
    const draft = { ...lane, publish: { ...lane.publish, plans: {} } };
    expect(minutesLate(draft, at("2026-10-08T14:59:00Z"), now)).toBeNull();
    // Delivered stops are skipped; the next pending stop's ETA counts.
    const two = published(
      "T1",
      [
        ["O1", STOP_A],
        ["O2", STOP_A],
      ],
      "2026-10-08T14:00:00Z",
    );
    two.loads[0].stops[0].snapshot.status = "delivered";
    two.loads[0].stops[1].eta = "2026-10-08T14:50:00Z";
    expect(minutesLate(two, at("2026-10-08T14:59:00Z"), now)).toBeNull();
    two.loads[0].stops[1].eta = "2026-10-08T14:40:00Z";
    expect(minutesLate(two, at("2026-10-08T14:59:00Z"), now)).toBe(20);
  });

  it("LaneRouteMap renders standalone with no stops", () => {
    render(
      <LaneRouteMap
        lanes={[makeLane("T9")]}
        selectedOrderIds={new Set()}
        onSelectStop={() => {}}
        positions={{}}
        terminals={{}}
        label="Empty map"
      />,
    );
    expect(
      screen.getByText("No stops with a location to show."),
    ).toBeInTheDocument();
  });
});
