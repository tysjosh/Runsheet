/**
 * Trays (plan task 26; R3.1–R3.6, R3.8, R4.1, R4.2). API, socket and
 * Pragmatic are mocked; nothing reaches a backend.
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
jest.mock("../../../services/dispatchBoardApi", () => ({
  ...jest.requireActual("../../../services/dispatchBoardApi"),
  getBoard: jest.fn(),
  sendBoardCommand: jest.fn(),
  validateBoard: jest.fn(),
}));
jest.mock("../../../services/ordersApi", () => ({
  ...jest.requireActual("../../../services/ordersApi"),
  releaseHoldOrder: jest.fn(),
}));
jest.mock("../../../utils/auth", () => ({
  ...jest.requireActual("../../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));

import { releaseHoldOrder } from "../../../services/ordersApi";
import { makeDriver, makeLane, makeTrayOrder } from "../state/testFixtures";
import {
  boardSnap,
  mockGetBoard,
  mockSend,
  okResponse,
  renderBoard,
  resetBoardMocks,
  sentBodies,
  TODAY,
} from "../testBoard";
import { navigation, resetMocks } from "../testMocks";
import { addDays } from "../viewState";

const mockRelease = releaseHoldOrder as unknown as jest.Mock;

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
  mockRelease.mockReset();
});

function trays(over: Parameters<typeof boardSnap>[0] = {}) {
  return boardSnap({ lanes: [makeLane("T1", 1)], ...over });
}

describe("order tray", () => {
  it("shows every R3.2 field on a card", async () => {
    await renderBoard(
      trays({
        trays: {
          orders: [
            makeTrayOrder("1042", {
              customer_name: "Acme Farms",
              product_code: "ULSD",
              gallons_requested: 3000,
              call_type: "keep_full",
              priority_bucket: "High",
              delivery_window_start: `${TODAY}T13:00:00Z`,
              delivery_window_end: `${TODAY}T17:00:00Z`,
              missing_window: false,
              dyed: true,
            }),
            makeTrayOrder("1043", {
              fill_to_full: true,
              gallons_requested: null,
              missing_window: true,
            }),
          ],
          orders_truncated: false,
          drivers: [],
          trucks: [],
        },
      }),
    );
    const list = screen.getByRole("listbox", { name: "Orders to plan" });
    const first = within(list).getAllByRole("option")[0];
    const name = first.getAttribute("aria-label") ?? "";
    for (const part of [
      "Order 1042",
      "Acme Farms",
      "ULSD",
      "3,000 gal",
      "8:00 AM–12:00 PM",
      "Keep full",
      "priority High",
      "dyed diesel",
    ]) {
      expect(name).toContain(part);
    }
    expect(within(first).getByText("Dyed")).toBeInTheDocument();
    const second = within(list).getAllByRole("option")[1];
    expect(second.getAttribute("aria-label")).toContain("Fill");
    expect(second.getAttribute("aria-label")).toContain("missing window");
    expect(within(second).getByText("No window")).toBeInTheDocument();
  });

  it("keeps the server order by default and sorts by customer on request (R3.3)", async () => {
    await renderBoard(
      trays({
        trays: {
          orders: [
            makeTrayOrder("O2", { customer_name: "Zed" }),
            makeTrayOrder("O1", { customer_name: "Abe" }),
          ],
          orders_truncated: false,
          drivers: [],
          trucks: [],
        },
      }),
    );
    const ids = () =>
      within(screen.getByRole("listbox", { name: "Orders to plan" }))
        .getAllByRole("option")
        .map((o) => o.getAttribute("data-focus-key"));
    expect(ids()).toEqual(["order:O2", "order:O1"]);
    fireEvent.change(screen.getByRole("combobox", { name: "Sort" }), {
      target: { value: "customer" },
    });
    expect(ids()).toEqual(["order:O1", "order:O2"]);
  });

  it("groups on-hold orders, collapsed and not draggable, with Release hold (R3.6)", async () => {
    mockRelease.mockResolvedValue({ status: "placed" });
    await renderBoard(
      trays({
        trays: {
          orders: [
            makeTrayOrder("O1"),
            makeTrayOrder("H1", {
              status: "on_hold",
              draggable: false,
              block_reason: "on_hold",
            }),
          ],
          orders_truncated: false,
          drivers: [],
          trucks: [],
        },
      }),
    );
    expect(
      screen.queryByRole("listbox", { name: "On hold orders" }),
    ).not.toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "On hold (1)" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    const held = within(
      screen.getByRole("listbox", { name: "On hold orders" }),
    ).getByRole("option");
    expect(held).not.toHaveAttribute("draggable");
    expect(held.querySelector("[data-drag-handle]")).toBeNull();
    // The ready card is draggable from its grip.
    const ready = within(
      screen.getByRole("listbox", { name: "Orders to plan" }),
    ).getByRole("option");
    expect(ready).toHaveAttribute("draggable", "true");

    fireEvent.click(held);
    const menu = screen.getByRole("menu", { name: "Order H1 actions" });
    const assign = within(menu).getByRole("menuitem", {
      name: /Assign to truck/,
    });
    expect(assign).toHaveAttribute("aria-disabled", "true");
    expect(assign).toHaveTextContent("Release the hold first");
    const calls = mockGetBoard.mock.calls.length;
    await act(async () => {
      fireEvent.click(
        within(menu).getByRole("menuitem", { name: "Release hold" }),
      );
    });
    expect(mockRelease).toHaveBeenCalledWith("H1");
    await waitFor(() =>
      expect(mockGetBoard.mock.calls.length).toBeGreaterThan(calls),
    );
    expect(mockSend).not.toHaveBeenCalled();
  });

  it("says when the list is truncated (R3.8)", async () => {
    await renderBoard(
      trays({
        trays: {
          orders: [makeTrayOrder("O1")],
          orders_truncated: true,
          drivers: [],
          trucks: [],
        },
      }),
    );
    expect(
      within(screen.getByRole("region", { name: "Order tray" })).getByText(
        "Showing the 1,000 earliest delivery windows. Filter to narrow the list.",
      ),
    ).toBeInTheDocument();
  });

  it("filter chips dim non-matching cards and search highlights matches, never removing them (R4.1, R4.2)", async () => {
    navigation.params = new URLSearchParams("product=ULSD&q=acme");
    await renderBoard(
      trays({
        trays: {
          orders: [
            makeTrayOrder("O1", {
              product_code: "ULSD",
              customer_name: "Acme",
            }),
            makeTrayOrder("O2", {
              product_code: "ULSD",
              customer_name: "Bolt",
            }),
            makeTrayOrder("O3", { product_code: "GAS", customer_name: "Acme" }),
          ],
          orders_truncated: false,
          drivers: [],
          trucks: [],
        },
      }),
    );
    const list = () => screen.getByRole("listbox", { name: "Orders to plan" });
    const card = (id: string) =>
      within(list())
        .getAllByRole("option")
        .find(
          (o) => o.getAttribute("data-focus-key") === `order:${id}`,
        ) as HTMLElement;
    expect(within(list()).getAllByRole("option")).toHaveLength(3);
    expect(card("O1")).toHaveAttribute("data-match", "match");
    expect(card("O2")).toHaveAttribute("data-match", "dim");
    expect(card("O3")).toHaveAttribute("data-match", "dim");
    expect(card("O3").getAttribute("aria-label")).toContain(
      "doesn't match the filters",
    );
  });
});

describe("driver tray", () => {
  it("lists qualification, HOS today and ineligible reasons (R3.4)", async () => {
    await renderBoard(
      trays({
        trays: {
          orders: [],
          orders_truncated: false,
          drivers: [
            makeDriver("D1", {
              name: "Ana",
              cdl_class: "A",
              hazmat_endorsement: true,
              paired_truck_id: "T1",
              hos: {
                remaining_drive_time: { availability: "available", value: 6.5 },
              },
            }),
            makeDriver("D2", {
              name: "Ben",
              eligible: false,
              ineligible_reasons: ["medical_card_expired: 2026-09-01"],
            }),
          ],
          trucks: [],
        },
      }),
    );
    fireEvent.click(screen.getByRole("tab", { name: /Drivers/ }));
    const list = screen.getByRole("listbox", { name: "Drivers" });
    const [ana, ben] = within(list).getAllByRole("option");
    expect(ana.getAttribute("aria-label")).toBe(
      "Ana, active, on Truck T1, CDL A, HAZMAT, 6.5 h driving left",
    );
    expect(ben.getAttribute("aria-label")).toContain(
      "can't be dispatched: medical card expired",
    );
    expect(within(ben).getByText("Not eligible")).toBeInTheDocument();
  });

  it("shows the tanker endorsement and the nearest expiry, and nothing without a DQ record (R3.4)", async () => {
    await renderBoard(
      trays({
        trays: {
          orders: [],
          orders_truncated: false,
          drivers: [
            makeDriver("D1", {
              name: "Ana",
              cdl_class: "A",
              hazmat_endorsement: false,
              tanker_endorsement: true,
              nearest_expiry: {
                kind: "medical_card",
                expires_on: "2999-11-02",
              },
            }),
            makeDriver("D2", {
              name: "Ben",
              cdl_class: null,
              hazmat_endorsement: false,
              tanker_endorsement: false,
              nearest_expiry: { kind: "tanker", expires_on: "2000-09-30" },
            }),
            makeDriver("D3", {
              name: "Cy",
              cdl_class: null,
              hazmat_endorsement: null,
            }),
          ],
          trucks: [],
        },
      }),
    );
    fireEvent.click(screen.getByRole("tab", { name: /Drivers/ }));
    const [ana, ben, cy] = within(
      screen.getByRole("listbox", { name: "Drivers" }),
    ).getAllByRole("option");
    expect(ana.getAttribute("aria-label")).toBe(
      "Ana, active, not paired, CDL A, Tanker, Medical card expires 2999-11-02",
    );
    expect(
      within(ana).getByText(/Medical card expires 2999-11-02/),
    ).toBeInTheDocument();
    expect(ben.getAttribute("aria-label")).toBe(
      "Ben, active, not paired, Tanker endorsement expired 2000-09-30",
    );
    expect(cy.getAttribute("aria-label")).toBe("Cy, active, not paired");
  });

  it("shows no HOS figure on a future day", async () => {
    navigation.params = new URLSearchParams(`date=${addDays(TODAY, 1)}`);
    await renderBoard(
      boardSnap({
        service_date: addDays(TODAY, 1),
        lanes: [makeLane("T1", 1)],
        trays: {
          orders: [],
          orders_truncated: false,
          drivers: [
            makeDriver("D1", {
              name: "Ana",
              hos: {
                remaining_drive_time: { availability: "available", value: 6.5 },
              },
            }),
          ],
          trucks: [],
        },
      }),
    );
    fireEvent.click(screen.getByRole("tab", { name: /Drivers/ }));
    expect(
      within(screen.getByRole("listbox", { name: "Drivers" }))
        .getByRole("option")
        .getAttribute("aria-label"),
    ).not.toContain("driving left");
  });
});

describe("truck tray", () => {
  it("Add lane sends add_lane (R3.5)", async () => {
    mockSend.mockResolvedValue(okResponse([makeLane("T9", 1)]));
    await renderBoard(
      trays({
        trays: {
          orders: [],
          orders_truncated: false,
          drivers: [],
          trucks: [{ truck_id: "T9", compartment_count: 4, capacity_l: 37854 }],
        },
      }),
    );
    fireEvent.click(screen.getByRole("tab", { name: /Trucks/ }));
    expect(screen.getByText("4 compartments · 10,000 gal")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Add lane for Truck T9" }),
      );
    });
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toEqual({
      type: "add_lane",
      truck_id: "T9",
      expected_lane_versions: { T9: 0 },
    });
  });
});
