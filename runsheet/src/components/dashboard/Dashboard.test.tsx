/**
 * Dashboard (UI revamp §7.1, R7): ported from the Today cockpit's tests
 * (sources load independently and fail open, inline assign / release, live
 * socket refresh) plus the new widgets: severity ranking with exceptions and
 * approvals, inline Approve, board deep links, Today's runs, Plan status and
 * the title-row counts.
 */
import "@testing-library/jest-dom";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/dashboard",
}));
jest.mock("../../services/ordersApi", () => ({
  listOrders: jest.fn(),
  assignDriver: jest.fn(),
  releaseHoldOrder: jest.fn(),
}));
jest.mock("../../services/schedulingApi", () => ({
  getDelayedJobs: jest.fn(),
  getActiveJobs: jest.fn(),
}));
jest.mock("../../services/fuelApi", () => ({
  getAlerts: jest.fn(),
  listPlans: jest.fn(),
}));
jest.mock("../../services/agentApi", () => ({
  getApprovals: jest.fn(),
  approveAction: jest.fn(),
}));
jest.mock("../../services/dispatchBoardApi", () => {
  const actual = jest.requireActual("../../services/dispatchBoardApi");
  return { ...actual, getBoardStatus: jest.fn(), getBoard: jest.fn() };
});
jest.mock("../../services/tenant", () => ({
  getCurrentTenantId: () => "tenant-a",
}));
jest.mock("../../hooks/useOrdersWebSocket", () => ({
  useOrdersWebSocket: jest.fn(),
}));
jest.mock("../../hooks/useSchedulingWebSocket", () => ({
  useSchedulingWebSocket: jest.fn(),
}));
jest.mock("../../services/complianceApi", () => ({
  getDrivers: jest.fn().mockResolvedValue({
    data: [
      {
        driver_id: "DRV-1",
        full_name: "Ada Lovelace",
        cdl_class: "A",
        status: "active",
      },
    ],
    page: 1,
    size: 200,
    total: 1,
    request_id: "d1",
  }),
}));

import { useOrdersWebSocket } from "../../hooks/useOrdersWebSocket";
import { useSchedulingWebSocket } from "../../hooks/useSchedulingWebSocket";
import { approveAction, getApprovals } from "../../services/agentApi";
import {
  BoardApiError,
  getBoard,
  getBoardStatus,
} from "../../services/dispatchBoardApi";
import { getAlerts, listPlans } from "../../services/fuelApi";
import {
  assignDriver,
  listOrders,
  releaseHoldOrder,
} from "../../services/ordersApi";
import { getActiveJobs, getDelayedJobs } from "../../services/schedulingApi";
import {
  makeLane,
  makeLoad,
  makeSnapshot,
} from "../dispatch-board/state/testFixtures";
import { addDays, todayIn } from "../dispatch-board/viewState";
import { resetToasts } from "../ui/toast/notify";
import Dashboard from "./Dashboard";
import { boardLink, rankAttention } from "./dashboardModel";

const m = <T,>(f: T) => f as unknown as jest.Mock;
const TODAY = todayIn("UTC");

const order = (
  id: string,
  status: string,
  extra: Record<string, unknown> = {},
) => ({
  order_id: id,
  customer_id: `C-${id}`,
  customer_name: `Customer ${id}`,
  ship_to_address: "1 Main St",
  product_code: "DIESEL_2",
  gallons_requested: 1200,
  fill_to_full: false,
  status,
  hold_reason: status === "on_hold" ? "credit hold" : null,
  delivery_window_start: null,
  delivery_window_end: null,
  created_at: "2026-10-08T07:00:00Z",
  assigned_driver_id: null,
  ...extra,
});

function setDefaults() {
  m(listOrders).mockResolvedValue({ items: [], total: 0 });
  m(getDelayedJobs).mockResolvedValue({ data: [] });
  m(getActiveJobs).mockResolvedValue({ data: [] });
  m(getAlerts).mockResolvedValue({ data: [] });
  m(getApprovals).mockResolvedValue({ entries: [] });
  m(listPlans).mockResolvedValue({ items: [], total: 0 });
  // Board off by default (404 = flag disabled).
  m(getBoardStatus).mockRejectedValue(new BoardApiError("disabled", 404));
  m(getBoard).mockResolvedValue(makeSnapshot({ service_date: TODAY }));
}

beforeEach(() => {
  jest.clearAllMocks();
  resetToasts();
  setDefaults();
});

async function renderDash() {
  const onCreateOrder = jest.fn();
  render(<Dashboard onCreateOrder={onCreateOrder} />);
  await waitFor(() =>
    expect(screen.queryByLabelText("Loading attention items")).toBeNull(),
  );
  return { onCreateOrder };
}

describe("title row", () => {
  it("has one h1, the service day and linked counts (R7.2)", async () => {
    m(getDelayedJobs).mockResolvedValue({
      data: [
        {
          job_id: "J1",
          destination: "Site A",
          delay_duration_minutes: 45,
          asset_assigned: "T1",
        },
      ],
    });
    m(getApprovals).mockResolvedValue({
      entries: [
        {
          action_id: "A1",
          action_type: "route_plan",
          impact_summary: "Route plan for 4 loads",
          proposed_by: "route agent",
          proposed_at: "2026-10-08T06:00:00Z",
          risk_level: "low",
          status: "pending",
        },
      ],
    });
    await renderDash();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Dashboard",
    );
    expect(
      screen.getByRole("button", { name: /^Today/, pressed: true }),
    ).toBeInTheDocument();
    const counts = screen.getByRole("list", { name: "Counts" });
    expect(
      within(counts).getByRole("link", { name: "1 delayed" }),
    ).toHaveAttribute("href", "/dashboard/dispatch?tab=jobs&status=delayed");
    expect(
      within(counts).getByRole("link", { name: "1 approval" }),
    ).toHaveAttribute("href", "/dashboard/control?tab=approvals");
    expect(
      within(counts).getByRole("link", { name: "0 exceptions" }),
    ).toHaveAttribute("href", "/dashboard/control");
  });
});

describe("Needs attention", () => {
  it("renders placed and on-hold orders with readable product names (R5.4)", async () => {
    m(listOrders).mockImplementation(({ status }: { status: string }) =>
      Promise.resolve({
        items:
          status === "placed"
            ? [order("O1", "placed")]
            : [order("O2", "on_hold")],
        total: 1,
      }),
    );
    await renderDash();
    const feed = screen.getByRole("list", { name: "Attention items" });
    expect(
      within(feed).getByRole("link", { name: "Customer O1" }),
    ).toHaveAttribute("href", "/dashboard/orders/O1");
    expect(feed).toHaveTextContent("1,200 gal · Diesel #2 (on-road)");
    expect(feed).not.toHaveTextContent("DIESEL_2");
    // On hold outranks placed.
    const rows = within(feed).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("On hold");
    expect(rows[1]).toHaveTextContent("Placed");
    expect(screen.getByRole("button", { name: /^Orders/ })).toHaveTextContent(
      "2",
    );
  });

  it("ranks an empty tank above an exception above a delay above approvals", () => {
    const ranked = rankAttention({
      exceptions: [
        {
          truckId: "T1",
          orderId: "O9",
          customerId: null,
          productCode: null,
          status: "failed",
        },
      ],
      delayed: [{ job_id: "J1", delay_duration_minutes: 30 } as never],
      approvals: [{ action_id: "A1", risk_level: "low" } as never],
      orders: [order("O1", "placed") as never],
      tanks: [
        { station_id: "S1", status: "empty", stock_percentage: 0 } as never,
      ],
    });
    expect(ranked.map((r) => r.kind)).toEqual([
      "tank",
      "exception",
      "delayed",
      "approval",
      "order",
    ]);
  });

  it("delays open the board lane when the board is on (R7.4)", async () => {
    m(getBoardStatus).mockResolvedValue({ mode: "active_gated" });
    m(getDelayedJobs).mockResolvedValue({
      data: [
        {
          job_id: "J1",
          destination: "Harbor Heating",
          delay_duration_minutes: 45,
          asset_assigned: "T2",
          order_id: "O7",
        },
      ],
    });
    await renderDash();
    const feed = screen.getByRole("list", { name: "Attention items" });
    expect(within(feed).getByText("+45 min")).toBeInTheDocument();
    expect(
      within(feed).getByRole("link", { name: "Open on board" }),
    ).toHaveAttribute(
      "href",
      boardLink({ date: TODAY, truck: "T2", order: "O7" }),
    );
  });

  it("board exceptions show with an icon and label and open their lane", async () => {
    m(getBoardStatus).mockResolvedValue({ mode: "active_gated" });
    const lane = makeLane("T3", 2, { loads: [makeLoad("L1", ["O5"])] });
    lane.loads[0].stops[0].snapshot.status = "failed";
    m(getBoard).mockResolvedValue(
      makeSnapshot({ service_date: TODAY, lanes: [lane] }),
    );
    await renderDash();
    const feed = screen.getByRole("list", { name: "Attention items" });
    const row = within(feed).getAllByRole("listitem")[0];
    expect(row).toHaveTextContent("Exception");
    expect(row.querySelector("svg")).not.toBeNull();
    expect(
      within(row).getByRole("link", { name: "Open on board" }),
    ).toHaveAttribute(
      "href",
      boardLink({ date: TODAY, truck: "T3", order: "O5" }),
    );
  });

  it("approves inline with the inbox's call, or opens Live at the item", async () => {
    m(getApprovals).mockResolvedValue({
      entries: [
        {
          action_id: "A1",
          action_type: "refill",
          impact_summary: "Refill proposal: Yard Gasoline",
          proposed_by: "fuel agent",
          risk_level: "medium",
          status: "pending",
        },
      ],
    });
    m(approveAction).mockResolvedValue({});
    await renderDash();
    expect(
      screen.getByRole("link", {
        name: "Review Refill proposal: Yard Gasoline",
      }),
    ).toHaveAttribute("href", "/dashboard/control?tab=approvals&id=A1");
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", {
          name: "Approve Refill proposal: Yard Gasoline",
        }),
      );
    });
    expect(approveAction).toHaveBeenCalledWith("A1");
    await waitFor(() =>
      expect(screen.queryByText("Refill proposal: Yard Gasoline")).toBeNull(),
    );
  });

  it("low tanks open the station and offer Create order", async () => {
    m(getAlerts).mockResolvedValue({
      data: [
        {
          station_id: "S1",
          name: "Yard Gasoline Tank",
          fuel_type: "GASOLINE_REG",
          status: "critical",
          stock_percentage: 12,
          days_until_empty: 0.6,
          current_stock_liters: 1,
          capacity_liters: 10,
          capacity_gallons: 10000,
          current_stock_gallons: 1200,
        },
      ],
    });
    const { onCreateOrder } = await renderDash();
    expect(screen.getByText("12% tank")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Yard Gasoline Tank" }),
    ).toHaveAttribute("href", "/dashboard/fuel-ops?tab=stations&station=S1");
    expect(screen.getByText(/runout in ~14 h/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create order" }));
    expect(onCreateOrder).toHaveBeenCalledWith(
      expect.objectContaining({
        product_code: "GASOLINE_REG",
        gallons_requested: "8800",
      }),
    );
    expect(screen.getByText(/8,800 gal to fill/)).toBeInTheDocument();
  });

  it("filters by category with counts in the chips", async () => {
    m(listOrders).mockResolvedValue({
      items: [order("O1", "placed")],
      total: 1,
    });
    m(getDelayedJobs).mockResolvedValue({
      data: [{ job_id: "J1", destination: "X", delay_duration_minutes: 10 }],
    });
    await renderDash();
    fireEvent.click(screen.getByRole("button", { name: /^Delayed/ }));
    const feed = screen.getByRole("list", { name: "Attention items" });
    expect(within(feed).getAllByRole("listitem")).toHaveLength(1);
    expect(feed).toHaveTextContent("Job J1");
  });

  it("stays usable when a source fails, and says so", async () => {
    m(getDelayedJobs).mockRejectedValue(new Error("boom"));
    m(listOrders).mockResolvedValue({
      items: [order("O1", "placed")],
      total: 1,
    });
    await renderDash();
    expect(screen.getByText("Customer O1")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "" })).toBeDefined();
    expect(screen.getByText(/One source/)).toBeInTheDocument();
  });

  it("shows a per-widget error with Retry when every source fails (R11.2)", async () => {
    m(listOrders).mockRejectedValue(new TypeError("Failed to fetch"));
    m(getDelayedJobs).mockRejectedValue(new TypeError("Failed to fetch"));
    m(getAlerts).mockRejectedValue(new TypeError("Failed to fetch"));
    m(getApprovals).mockRejectedValue(new TypeError("Failed to fetch"));
    await renderDash();
    expect(
      screen.getByText(
        "Can't reach Runsheet. Check your connection and retry.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Failed to fetch")).toBeNull();
    m(listOrders).mockResolvedValue({ items: [], total: 0 });
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(listOrders).toHaveBeenCalledTimes(4));
  });

  it("tolerates a null list payload", async () => {
    m(listOrders).mockResolvedValue({ items: null, total: 0 });
    await renderDash();
    expect(screen.getByText("You're all caught up")).toBeInTheDocument();
  });

  it("assigns a driver inline without the board and removes the order", async () => {
    m(listOrders).mockImplementation(({ status }: { status: string }) =>
      Promise.resolve(
        status === "placed"
          ? { items: [order("O1", "placed")], total: 1 }
          : { items: [], total: 0 },
      ),
    );
    m(assignDriver).mockResolvedValue({});
    await renderDash();
    fireEvent.click(screen.getByRole("button", { name: "Assign" }));
    fireEvent.click(await screen.findByLabelText("Driver"));
    fireEvent.click(await screen.findByText("Ada Lovelace"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    });
    expect(assignDriver).toHaveBeenCalledWith("O1", { driver_id: "DRV-1" });
    await waitFor(() => expect(screen.queryByText("Customer O1")).toBeNull());
  });

  it("with the board on, placed orders get Assign on board", async () => {
    m(getBoardStatus).mockResolvedValue({ mode: "active_gated" });
    m(listOrders).mockImplementation(({ status }: { status: string }) =>
      Promise.resolve(
        status === "placed"
          ? { items: [order("O1", "placed")], total: 1 }
          : { items: [], total: 0 },
      ),
    );
    await renderDash();
    expect(
      screen.getByRole("link", { name: "Assign on board" }),
    ).toHaveAttribute("href", boardLink({ date: TODAY, order: "O1" }));
  });

  it("releases a hold inline", async () => {
    m(listOrders).mockImplementation(({ status }: { status: string }) =>
      Promise.resolve(
        status === "on_hold"
          ? { items: [order("O2", "on_hold")], total: 1 }
          : { items: [], total: 0 },
      ),
    );
    m(releaseHoldOrder).mockResolvedValue({ status: "placed" });
    await renderDash();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Release" }));
    });
    expect(releaseHoldOrder).toHaveBeenCalledWith("O2");
    await waitFor(() => expect(screen.queryByText("Customer O2")).toBeNull());
  });
});

describe("Today's runs and Plan status", () => {
  it("one row per truck with progress, opening the lane (R7.3)", async () => {
    m(getBoardStatus).mockResolvedValue({ mode: "active_gated" });
    const lane = makeLane("T1", 2, { loads: [makeLoad("L1", ["O1", "O2"])] });
    lane.loads[0].stops[0].snapshot.status = "delivered";
    lane.loads[0].stops[1].snapshot.status = "in_transit";
    m(getBoard).mockImplementation((d: string) =>
      Promise.resolve(
        makeSnapshot({
          service_date: d,
          lanes:
            d === TODAY
              ? [lane]
              : [makeLane("T9", 1, { loads: [makeLoad("L9", ["O9"])] })],
        }),
      ),
    );
    await renderDash();
    const runs = screen.getByRole("list", { name: "Today's runs" });
    const link = within(runs).getByRole("link");
    expect(link).toHaveAttribute(
      "href",
      boardLink({ date: TODAY, truck: "T1" }),
    );
    expect(link).toHaveAccessibleName(
      /Truck T1.*In transit, 1 of 2 stops delivered/,
    );
    // Plan status: tomorrow's draft lane with a Review on board link.
    const plans = screen.getByRole("list", { name: "Plans" });
    expect(plans).toHaveTextContent("Draft plan");
    expect(plans).toHaveTextContent("Tomorrow · 1 load on 1 truck");
    expect(
      within(plans).getAllByRole("link", { name: "Review on board" }).at(-1),
    ).toHaveAttribute("href", boardLink({ date: addDays(TODAY, 1) }));
    // Tomorrow toggles the runs widget to tomorrow's board.
    fireEvent.click(screen.getByRole("button", { name: /^Tomorrow/ }));
    expect(
      screen.getByRole("list", { name: "Tomorrow's runs" }),
    ).toHaveTextContent("T9");
  });

  it("without the board, runs come from active jobs and plans from the plan list", async () => {
    m(getActiveJobs).mockResolvedValue({
      data: [
        {
          job_id: "J1",
          status: "in_progress",
          asset_assigned: "TRK-1",
          delayed: false,
        },
        {
          job_id: "J2",
          status: "completed",
          asset_assigned: "TRK-1",
          delayed: false,
        },
      ],
    });
    m(listPlans).mockResolvedValue({
      items: [
        {
          plan_id: "P1",
          status: "draft",
          truck_id: "TRK-1",
          created_at: "x",
          total_utilization_pct: 50,
        },
      ],
    });
    await renderDash();
    expect(
      screen.getByRole("list", { name: "Today's runs" }),
    ).toHaveTextContent("TRK-1");
    expect(screen.getByRole("list", { name: "Plans" })).toHaveTextContent(
      "Draft",
    );
    expect(getBoard).not.toHaveBeenCalled();
  });
});

describe("live updates", () => {
  it("subscribes to the orders and scheduling sockets and refetches on events", async () => {
    jest.useFakeTimers();
    try {
      render(<Dashboard />);
      await act(async () => {
        await Promise.resolve();
      });
      expect(useOrdersWebSocket).toHaveBeenCalled();
      expect(useSchedulingWebSocket).toHaveBeenCalled();
      const opts = m(useOrdersWebSocket).mock.calls.at(-1)[1];
      const before = m(listOrders).mock.calls.length;
      act(() => opts.onOrderPlaced());
      await act(async () => {
        jest.advanceTimersByTime(600);
      });
      await waitFor(() =>
        expect(m(listOrders).mock.calls.length).toBeGreaterThan(before),
      );
      const sched = m(useSchedulingWebSocket).mock.calls.at(-1)[0];
      const mid = m(listOrders).mock.calls.length;
      act(() => sched.onDelayAlert());
      await act(async () => {
        jest.advanceTimersByTime(600);
      });
      await waitFor(() =>
        expect(m(listOrders).mock.calls.length).toBeGreaterThan(mid),
      );
    } finally {
      jest.useRealTimers();
    }
  });
});
