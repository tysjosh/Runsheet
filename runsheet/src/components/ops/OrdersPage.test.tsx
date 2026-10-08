/**
 * Tests for OrdersPage component.
 *
 * Covers:
 * - Happy path: list fetch + render
 * - Filter changes trigger refetch
 * - Pagination controls
 * - Intake channel badge rendering
 * - WebSocket update reception
 * - Error state rendering
 * - Empty state rendering
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

// Mock the ordersApi module
jest.mock("../../services/ordersApi", () => ({
  listOrders: jest.fn(),
}));

// Mock the WebSocket hook
jest.mock("../../hooks/useOrdersWebSocket", () => ({
  useOrdersWebSocket: jest.fn(() => ({
    state: "connected",
    isConnected: true,
    reconnectAttempt: 0,
    lastOrderUpdate: null,
    error: null,
    connect: jest.fn(),
    disconnect: jest.fn(),
    send: jest.fn(),
  })),
}));

// Export CSV button: stub the download and the session roles.
jest.mock("../../services/exportApi", () => ({
  downloadCsvExport: jest.fn(),
}));
jest.mock("../../utils/auth", () => ({
  ...jest.requireActual("../../utils/auth"),
  getCurrentUserRoles: jest.fn(async () => []),
}));

import { useOrdersWebSocket } from "../../hooks/useOrdersWebSocket";
import { downloadCsvExport } from "../../services/exportApi";
import type { FuelOrder, OrderListResponse } from "../../services/ordersApi";
import { listOrders } from "../../services/ordersApi";
import { getCurrentUserRoles } from "../../utils/auth";
import OrdersPage from "./OrdersPage";

const mockListOrders = listOrders as jest.MockedFunction<typeof listOrders>;
const mockUseOrdersWebSocket = useOrdersWebSocket as jest.MockedFunction<
  typeof useOrdersWebSocket
>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function orderFixture(overrides: Partial<FuelOrder> = {}): FuelOrder {
  return {
    order_id: "ord_abc123def456",
    tenant_id: "tenant-a",
    customer_id: "CUST-001",
    customer_name: "Acme Fuel Co",
    customer_phone: "555-0100",
    customer_email: "acme@example.com",
    ship_to_address: "123 Main St",
    ship_to_lat: 40.7128,
    ship_to_lon: -74.006,
    customer_tank_id: null,
    product_code: "DIESEL_2",
    gallons_requested: 500,
    fill_to_full: false,
    call_type: "one_off",
    delivery_window_start: "2024-06-01T08:00:00Z",
    delivery_window_end: "2024-06-01T17:00:00Z",
    hold_reason: null,
    po_number: null,
    special_instructions: null,
    intake_channel: "dispatcher",
    intake_channel_id: "dispatcher",
    intake_metadata: { dispatcher_user_id: "user-1", session_id: "sess-1" },
    status: "placed",
    assigned_driver_id: null,
    assigned_run_id: null,
    legacy_origin_snapshot: null,
    source_schema_version: "1.0",
    trace_id: "trace-001",
    created_at: "2024-06-01T08:00:00Z",
    updated_at: "2024-06-01T08:00:00Z",
    last_event_timestamp: "2024-06-01T08:00:00Z",
    ...overrides,
  };
}

function paginatedResponse(
  orders: FuelOrder[],
  overrides: Partial<OrderListResponse> = {},
): OrderListResponse {
  return {
    items: orders,
    total: orders.length,
    page: 1,
    size: 20,
    ...overrides,
  };
}

beforeEach(() => {
  mockListOrders.mockReset();
  mockUseOrdersWebSocket.mockReturnValue({
    state: "connected" as any,
    isConnected: true,
    reconnectAttempt: 0,
    lastOrderUpdate: null,
    error: null,
    connect: jest.fn(),
    disconnect: jest.fn(),
    send: jest.fn(),
  });
});

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("OrdersPage — list render", () => {
  it("fetches orders on mount and renders rows", async () => {
    mockListOrders.mockResolvedValue(
      paginatedResponse([
        orderFixture({ order_id: "ord_111" }),
        orderFixture({ order_id: "ord_222", customer_name: "Beta Corp" }),
      ]),
    );

    render(<OrdersPage tenantId="tenant-a" />);

    await waitFor(() => expect(mockListOrders).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Acme Fuel Co")).toBeInTheDocument();
    expect(screen.getByText("Beta Corp")).toBeInTheDocument();
  });

  it("renders empty state when no orders returned", async () => {
    mockListOrders.mockResolvedValue(paginatedResponse([]));

    render(<OrdersPage tenantId="tenant-a" />);

    expect(await screen.findByText(/no orders found/i)).toBeInTheDocument();
  });

  it("renders error state on fetch failure", async () => {
    mockListOrders.mockRejectedValue(new Error("Network error"));

    render(<OrdersPage tenantId="tenant-a" />);

    expect(await screen.findByText(/network error/i)).toBeInTheDocument();
  });
});

describe("OrdersPage — customer linkage", () => {
  it("links the customer cell to the commerce customer record (optimistic on customer_id)", async () => {
    mockListOrders.mockResolvedValue(
      paginatedResponse([
        orderFixture({
          customer_id: "CUST-001",
          customer_name: "Acme Fuel Co",
        }),
      ]),
    );

    render(<OrdersPage tenantId="tenant-a" />);

    const link = await screen.findByRole("link", { name: "Acme Fuel Co" });
    expect(link).toHaveAttribute("href", "/dashboard/customers/CUST-001");
  });

  it("shows an Unlinked badge when the order has no customer_id", async () => {
    mockListOrders.mockResolvedValue(
      paginatedResponse([
        orderFixture({ customer_id: "", customer_name: "Stale Snapshot" }),
      ]),
    );

    render(<OrdersPage tenantId="tenant-a" />);

    expect(await screen.findByText(/unlinked/i)).toBeInTheDocument();
    // The row must not be a navigable customer link.
    expect(
      screen.queryByRole("link", { name: /stale snapshot/i }),
    ).not.toBeInTheDocument();
  });

  it("prefers the resolved customer name over the snapshot and links to the customer", async () => {
    mockListOrders.mockResolvedValue(
      paginatedResponse([
        orderFixture({
          customer_id: "CUST-OLD",
          customer_name: "Old Snapshot Name",
          links: {
            customer: {
              status: "resolved",
              id: "CUST-7",
              summary: { display_name: "Acme Fuels (current)" },
            },
          },
        }),
      ]),
    );

    render(<OrdersPage tenantId="tenant-a" />);

    const link = await screen.findByRole("link", {
      name: "Acme Fuels (current)",
    });
    expect(link).toHaveAttribute("href", "/dashboard/customers/CUST-7");
    // The stale snapshot name is not rendered.
    expect(screen.queryByText("Old Snapshot Name")).not.toBeInTheDocument();
  });

  it("shows an Unlinked affordance for an explicitly unresolved customer reference", async () => {
    mockListOrders.mockResolvedValue(
      paginatedResponse([
        orderFixture({
          customer_id: "CUST-MISSING",
          customer_name: "Ghost Co",
          links: { customer: { status: "unresolved", id: "CUST-MISSING" } },
        }),
      ]),
    );

    render(<OrdersPage tenantId="tenant-a" />);

    expect(await screen.findByText(/unlinked/i)).toBeInTheDocument();
    // No navigation offered for a dangling reference.
    expect(
      screen.queryByRole("link", { name: /ghost co/i }),
    ).not.toBeInTheDocument();
  });
});

describe("OrdersPage — intake channel badge", () => {
  it("shows an intake_channel badge on every order row", async () => {
    mockListOrders.mockResolvedValue(
      paginatedResponse([
        orderFixture({ intake_channel: "voice" }),
        orderFixture({ order_id: "ord_333", intake_channel: "csv" }),
      ]),
    );

    render(<OrdersPage tenantId="tenant-a" />);

    await waitFor(() => expect(mockListOrders).toHaveBeenCalled());
    const badges = await screen.findAllByTestId("intake-channel-badge");
    expect(badges.length).toBe(2);
    expect(badges[0]).toHaveTextContent("voice");
    expect(badges[1]).toHaveTextContent("csv");
  });
});

describe("OrdersPage — filters", () => {
  it("re-fetches with status filter when changed", async () => {
    mockListOrders.mockResolvedValue(paginatedResponse([orderFixture()]));

    render(<OrdersPage tenantId="tenant-a" />);

    await waitFor(() => expect(mockListOrders).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText(/filter by status/i), {
      target: { value: "delivered" },
    });

    await waitFor(() => expect(mockListOrders).toHaveBeenCalledTimes(2));
    const lastCall = mockListOrders.mock.calls[1][0];
    expect(lastCall).toEqual(expect.objectContaining({ status: "delivered" }));
  });

  it("re-fetches with intake_channel filter when changed", async () => {
    mockListOrders.mockResolvedValue(paginatedResponse([orderFixture()]));

    render(<OrdersPage tenantId="tenant-a" />);

    await waitFor(() => expect(mockListOrders).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText(/filter by intake channel/i), {
      target: { value: "voice" },
    });

    await waitFor(() => expect(mockListOrders).toHaveBeenCalledTimes(2));
    const lastCall = mockListOrders.mock.calls[1][0];
    expect(lastCall).toEqual(
      expect.objectContaining({ intake_channel: "voice" }),
    );
  });

  it("'Awaiting confirmation' asks for on_hold portal requests and toggles off (PD24)", async () => {
    mockListOrders.mockResolvedValue(paginatedResponse([orderFixture()]));

    render(<OrdersPage tenantId="tenant-a" />);
    await waitFor(() => expect(mockListOrders).toHaveBeenCalledTimes(1));

    const quick = screen.getByRole("button", { name: "Awaiting confirmation" });
    expect(quick).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(quick);

    await waitFor(() => expect(mockListOrders).toHaveBeenCalledTimes(2));
    expect(mockListOrders.mock.calls[1][0]).toEqual(
      expect.objectContaining({
        status: "on_hold",
        hold_reason: "awaiting_dispatcher_confirmation",
        page: 1,
      }),
    );
    expect(quick).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText(/filter by status/i)).toHaveValue("on_hold");

    // A manual status choice replaces the quick filter.
    fireEvent.change(screen.getByLabelText(/filter by status/i), {
      target: { value: "placed" },
    });
    await waitFor(() => expect(mockListOrders).toHaveBeenCalledTimes(3));
    const last = mockListOrders.mock.calls[2][0];
    expect(last).toEqual(expect.objectContaining({ status: "placed" }));
    expect(last?.hold_reason).toBeUndefined();
    expect(quick).toHaveAttribute("aria-pressed", "false");
  });
});

describe("OrdersPage — pagination", () => {
  it("shows pagination and navigates to next page", async () => {
    mockListOrders.mockResolvedValue(
      paginatedResponse([orderFixture()], {
        total: 40,
        page: 1,
        size: 20,
      }),
    );

    render(<OrdersPage tenantId="tenant-a" />);

    // Wait for the data to load and render
    await waitFor(() => expect(mockListOrders).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Acme Fuel Co")).toBeInTheDocument();

    const nextBtn = await screen.findByRole("button", { name: /next page/i });
    fireEvent.click(nextBtn);

    await waitFor(() => expect(mockListOrders).toHaveBeenCalledTimes(2));
    const lastCall = mockListOrders.mock.calls[1][0];
    expect(lastCall?.page).toBe(2);
  });
});

describe("OrdersPage — WebSocket updates", () => {
  it("passes callbacks to useOrdersWebSocket", () => {
    mockListOrders.mockResolvedValue(paginatedResponse([]));

    render(<OrdersPage tenantId="tenant-a" />);

    expect(mockUseOrdersWebSocket).toHaveBeenCalledWith(
      "tenant-a",
      expect.objectContaining({
        onOrderPlaced: expect.any(Function),
        onOrderStatusChanged: expect.any(Function),
        onOrderAssigned: expect.any(Function),
      }),
    );
  });

  it("updates order in list when WebSocket delivers a status change", async () => {
    const order = orderFixture({ order_id: "ord_ws1", status: "placed" });
    mockListOrders.mockResolvedValue(paginatedResponse([order]));

    let capturedCallbacks: any = {};
    mockUseOrdersWebSocket.mockImplementation((_tenantId, options) => {
      capturedCallbacks = options || {};
      return {
        state: "connected" as any,
        isConnected: true,
        reconnectAttempt: 0,
        lastOrderUpdate: null,
        error: null,
        connect: jest.fn(),
        disconnect: jest.fn(),
        send: jest.fn(),
      };
    });

    render(<OrdersPage tenantId="tenant-a" />);

    await waitFor(() => expect(mockListOrders).toHaveBeenCalled());
    expect(await screen.findByText("placed")).toBeInTheDocument();

    // Simulate WebSocket update
    act(() => {
      capturedCallbacks.onOrderStatusChanged?.({
        ...order,
        status: "dispatched",
      });
    });

    expect(await screen.findByText("dispatched")).toBeInTheDocument();
  });
});

describe("OrdersPage — Export CSV", () => {
  const mockDownload = downloadCsvExport as jest.MockedFunction<
    typeof downloadCsvExport
  >;
  const mockRoles = getCurrentUserRoles as jest.MockedFunction<
    typeof getCurrentUserRoles
  >;

  beforeEach(() => {
    mockDownload.mockReset();
    mockDownload.mockResolvedValue({ filename: "orders.csv" });
    mockListOrders.mockResolvedValue(paginatedResponse([orderFixture()]));
  });

  it("is hidden for a driver", async () => {
    mockRoles.mockResolvedValue(["driver"]);
    render(<OrdersPage tenantId="tenant-a" />);
    await waitFor(() => expect(mockRoles).toHaveBeenCalled());
    await act(async () => {});
    expect(
      screen.queryByRole("button", { name: /Export CSV/ }),
    ).not.toBeInTheDocument();
  });

  it("exports the current filters without page/size", async () => {
    mockRoles.mockResolvedValue(["dispatcher"]);
    render(<OrdersPage tenantId="tenant-a" />);
    const button = await screen.findByRole("button", {
      name: /^Export CSV ?: orders$/,
    });
    fireEvent.change(screen.getByLabelText(/filter by status/i), {
      target: { value: "delivered" },
    });
    await waitFor(() => expect(mockListOrders).toHaveBeenCalledTimes(2));
    await act(async () => {
      fireEvent.click(button);
    });
    expect(mockDownload).toHaveBeenCalledTimes(1);
    const [type, params] = mockDownload.mock.calls[0];
    expect(type).toBe("orders");
    expect(params).toEqual(expect.objectContaining({ status: "delivered" }));
    expect(params).not.toHaveProperty("page");
    expect(params).not.toHaveProperty("size");
  });
});
