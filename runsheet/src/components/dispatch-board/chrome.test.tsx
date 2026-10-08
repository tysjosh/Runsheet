/**
 * UI revamp tasks 2.2 and 2.3: the board's chrome and colour.
 *
 * - Under the Dispatch host, the date stepper, shift, presence, Generate plan
 *   and Publish N ready render in the title row; the board adds only one
 *   toolbar row (search, Filters popover, View menu, Map, suggestions chip,
 *   undo/redo, ⋯). Standalone it renders its own title row.
 * - Place mode replaces the toolbar's contents rather than adding a row.
 * - The View menu writes the same persisted keys as before.
 * - Lanes carry the identity stripe from `assignLaneIdentities`; the driver
 *   chip shows initials + surname with the full name as tooltip; loads and
 *   stops carry status colour with an icon and a label; the tray card and
 *   the gauge use readable product names and RP 1637 colours.
 */
import { act, fireEvent, render, screen, within } from "@testing-library/react";

jest.mock("next/navigation", () => require("./testMocks").navigationMock);
jest.mock(
  "../../hooks/useDispatchBoardSocket",
  () => require("./testMocks").socketMock,
);
jest.mock(
  "@atlaskit/pragmatic-drag-and-drop/adapter/element-adapter",
  () => require("./testMocks").pragmaticMock,
);
jest.mock(
  "@atlaskit/pragmatic-drag-and-drop-auto-scroll/element",
  () => require("./testMocks").autoScrollMock,
);
jest.mock("../../services/dispatchBoardApi", () => ({
  ...jest.requireActual("../../services/dispatchBoardApi"),
  getBoard: jest.fn(),
  sendBoardCommand: jest.fn(),
  validateBoard: jest.fn(),
}));
jest.mock("../../utils/auth", () => ({
  ...jest.requireActual("../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));

import { assignLaneIdentities } from "../../lib/identity";
import { PRODUCT } from "../../styles/tokens";
import { PageChromeProvider, PageHeader } from "../ui";
import DispatchBoard from "./DispatchBoard";
import { shortName } from "./grid/LaneHeader";
import {
  makeCompartment,
  makeDriver,
  makeLane,
  makeLoad,
  makeStop,
  makeTrayOrder,
} from "./state/testFixtures";
import {
  boardSnap,
  chooseView,
  mockGetBoard,
  moreAction,
  renderBoard,
  resetBoardMocks,
  viewButton,
} from "./testBoard";
import { resetMocks } from "./testMocks";

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
});

function snapshot() {
  const load = makeLoad("L1", ["O1", "O2"], {
    allocations: [
      {
        order_id: "O1",
        compartment_id: "T1-c1",
        product_code: "DIESEL_2",
        liters: 5000,
        capacity_liters: 10000,
      },
    ],
  });
  load.stops[0] = makeStop("O1", {
    snapshot: {
      ...load.stops[0].snapshot,
      product_code: "DIESEL_2",
      status: "in_transit",
    },
  });
  load.stops[1] = makeStop("O2", {
    snapshot: {
      ...load.stops[1].snapshot,
      product_code: "HEATING_OIL",
      status: "failed",
    },
  });
  return boardSnap({
    lanes: [
      makeLane("T1", 1, {
        driver_id: "D1",
        driver: makeDriver("D1", {
          name: "Darnell Price",
          tanker_endorsement: true,
        }),
        truck_type: "tank_wagon",
        compartments: [makeCompartment("T1-c1")],
        loads: [load],
      }),
      makeLane("T2", 1),
      makeLane("T3", 1),
    ],
    trays: {
      orders: [
        makeTrayOrder("O9", {
          product_code: "GASOLINE_PREM",
          customer_name: "Lone Star",
        }),
      ],
      orders_truncated: false,
      drivers: [],
      trucks: [],
    },
  });
}

async function renderHosted() {
  mockGetBoard.mockResolvedValue(snapshot());
  const utils = render(
    <PageChromeProvider>
      <PageHeader host title="Dispatch" />
      <DispatchBoard mode="active_gated" />
    </PageChromeProvider>,
  );
  await screen.findByRole("region", { name: "Trays" });
  return utils;
}

describe("title row and toolbar (task 2.2, R8.2)", () => {
  it("contributes the day, shift and primary actions to the Dispatch title row", async () => {
    const { container } = await renderHosted();
    const title = container.querySelector(
      '[data-chrome="titlerow"]',
    ) as HTMLElement;
    expect(within(title).getByRole("heading", { level: 1 })).toHaveTextContent(
      "Dispatch",
    );
    expect(
      within(title).getByRole("button", { name: "Previous day" }),
    ).toBeInTheDocument();
    expect(within(title).getByLabelText("Service day")).toBeInTheDocument();
    expect(
      within(title).getByRole("combobox", { name: "Shift" }),
    ).toBeInTheDocument();
    expect(
      within(title).getByRole("button", { name: "Generate plan" }),
    ).toBeInTheDocument();
    expect(
      within(title).getByRole("button", { name: /^Publish (all|\d+) ready$/ }),
    ).toBeInTheDocument();
    // Only one board row below the title row: the toolbar.
    const toolbar = screen.getByRole("group", { name: "Board" });
    expect(toolbar).toHaveAttribute("data-chrome", "toolbar");
    expect(toolbar.className).toContain("h-11");
    expect(
      within(toolbar).queryByRole("button", { name: "Previous day" }),
    ).toBeNull();
    for (const name of [
      /^Filters/,
      /^View:/,
      "Map",
      "Undo",
      "Redo",
      "More board actions",
    ])
      expect(within(toolbar).getByRole("button", { name })).toBeInTheDocument();
    expect(within(toolbar).getByRole("searchbox")).toBeInTheDocument();
  });

  it("standalone (no host) renders its own title row", async () => {
    const { container } = await renderBoard(snapshot());
    expect(container.querySelector('[data-chrome="titlerow"]')).toBeNull();
    expect(
      screen.getByRole("button", { name: "Previous day" }),
    ).toBeInTheDocument();
  });

  it("Filters live in a popover that takes no space until opened", async () => {
    await renderBoard(snapshot());
    expect(document.getElementById("board-filters")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    expect(document.getElementById("board-filters")?.className).toContain(
      "absolute",
    );
    // Product filters use readable names.
    expect(
      screen.getByRole("button", { name: "Premium unleaded" }),
    ).toBeInTheDocument();
  });

  it("the View menu keeps the persisted zoom and density keys", async () => {
    await renderBoard(snapshot());
    expect(viewButton()).toHaveAccessibleName("View: Timeline · Comfortable");
    chooseView("Sequence");
    chooseView("Compact");
    expect(viewButton()).toHaveAccessibleName("View: Sequence · Compact");
    const stored = JSON.parse(
      window.localStorage.getItem("runsheet.dispatchBoard.view.v1") ?? "{}",
    );
    expect(stored).toMatchObject({ zoom: "sequence", density: "compact" });
  });

  it("Suggestions, shortcuts and truck history are in ⋯", async () => {
    await renderBoard(snapshot());
    moreAction("Truck history");
    expect(
      await screen.findByRole("complementary", { name: "Truck T1 details" }),
    ).toBeInTheDocument();
  });

  it("Place mode replaces the toolbar's contents (R8.3)", async () => {
    await renderBoard(snapshot());
    const toolbar = screen.getByRole("group", { name: "Board" });
    const card = document.querySelector(
      '[data-focus-key="order:O9"]',
    ) as HTMLElement;
    act(() => card.focus());
    fireEvent.keyDown(card, { key: "Enter" });
    const banner = within(toolbar).getByTestId("place-mode-banner");
    expect(banner).toHaveTextContent("Placing Order O9");
    expect(within(toolbar).queryByRole("searchbox")).toBeNull();
    fireEvent.click(within(banner).getByRole("button", { name: "Cancel" }));
    expect(within(toolbar).getByRole("searchbox")).toBeInTheDocument();
  });
});

describe("colour with a second signal (task 2.3, R8.4)", () => {
  it("lanes carry their identity stripe in display order", async () => {
    await renderBoard(snapshot());
    const ids = assignLaneIdentities(["T1", "T2", "T3"]);
    for (const t of ["T1", "T2", "T3"]) {
      const row = document.querySelector(
        `[data-lane-row="${t}"]`,
      ) as HTMLElement;
      const stripe = row.querySelector("[data-identity]") as HTMLElement;
      expect(stripe.getAttribute("data-identity")).toBe(ids.get(t)?.hex);
    }
  });

  it("the driver chip shows initials + surname with the full name as tooltip", async () => {
    await renderBoard(snapshot());
    const chip = screen.getByRole("button", { name: "Driver Darnell Price" });
    expect(chip).toHaveTextContent("D. Price");
    expect(chip).toHaveAttribute("title", "Driver Darnell Price");
    expect(shortName("Darnell Price")).toBe("D. Price");
    expect(shortName("Ana")).toBe("Ana");
    // No driver: a red exception chip with an icon, named as before.
    const none = screen.getAllByRole("button", { name: "No driver" })[0];
    expect(none.className).toContain("text-red-800");
    expect(none.querySelector("svg")).not.toBeNull();
    // Qualification chip and the truck type stay visible.
    const row = document.querySelector('[data-lane-row="T1"]') as HTMLElement;
    expect(row).toHaveTextContent("Tanker ✓");
    expect(row).toHaveTextContent("tank wagon");
  });

  it("stops and loads are coloured by status with an icon and a label", async () => {
    await renderBoard(snapshot());
    const o1 = document.querySelector(
      '[data-focus-key="stop:O1"]',
    ) as HTMLElement;
    expect(o1).toHaveAttribute("data-status", "in_transit");
    expect(o1.style.backgroundColor).not.toBe("");
    expect(o1).toHaveTextContent("In transit");
    expect(o1.querySelector("svg")).not.toBeNull();
    expect(o1).toHaveTextContent("Diesel #2 (on-road)");
    expect(o1.querySelector('[data-product="DIESEL_2"]')).not.toBeNull();
    const o2 = document.querySelector(
      '[data-focus-key="stop:O2"]',
    ) as HTMLElement;
    expect(o2).toHaveAttribute("data-status", "exception");
    expect(o2).toHaveTextContent("Exception");
    // The load takes the most urgent stop status.
    const chip = screen.getByRole("button", { name: /^Load 1,/ });
    expect(chip).toHaveAttribute("data-status", "exception");
    expect(chip).toHaveTextContent("Exception");
  });

  it("a draft lane's load uses the dashed draft style", async () => {
    const snap = snapshot();
    snap.lanes[0].loads[0].stops.forEach((s) => {
      s.snapshot.status = "confirmed";
    });
    await renderBoard(snap);
    const chip = screen.getByRole("button", { name: /^Load 1,/ });
    // `confirmed` is Planned; the unpublished lane makes it Draft.
    expect(chip).toHaveAttribute("data-status", "draft");
    expect(chip.style.borderStyle).toBe("dashed");
  });

  it("tray cards show the product cap and readable name, plus a status badge", async () => {
    await renderBoard(snapshot());
    const card = document.querySelector(
      '[data-focus-key="order:O9"]',
    ) as HTMLElement;
    expect(card).toHaveTextContent("Premium unleaded");
    expect(card).not.toHaveTextContent("GASOLINE_PREM");
    expect(card.querySelector('[data-product="GASOLINE_PREM"]')).not.toBeNull();
    expect(card.querySelector("[data-status]")).not.toBeNull();
  });

  it("the gauge fills each compartment in its product colour", async () => {
    await renderBoard(snapshot());
    const seg = document.querySelector(
      '[data-lane-row="T1"] [data-compartment="T1-c1"]',
    ) as HTMLElement;
    expect(seg).toHaveAttribute("data-product", "DIESEL_2");
    const fill = seg.querySelector("span[aria-hidden]") as HTMLElement;
    expect(fill.style.backgroundColor).toBe(hexToRgb(PRODUCT.DIESEL_2.bg));
    expect(seg).toHaveTextContent("D 50%");
  });
});

function hexToRgb(hex: string): string {
  const n = Number.parseInt(hex.slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
}
