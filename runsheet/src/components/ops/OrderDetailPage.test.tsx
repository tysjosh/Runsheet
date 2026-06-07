/**
 * Tests for Order Detail Page.
 *
 * Covers:
 * - Happy path: order + events render
 * - Intake metadata channel-specific rendering (voice, dispatcher, csv)
 * - Storm mode banner
 * - Assigned driver card
 * - POD section when delivered
 * - Mutation controls: assign, cancel, status change
 * - Error handling on mutations
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

// Mock next/navigation
jest.mock("next/navigation", () => ({
  useParams: () => ({ orderId: "ord_test123" }),
  useRouter: () => ({ back: jest.fn(), push: jest.fn() }),
}));

jest.mock("../../services/ordersApi", () => ({
  getOrder: jest.fn(),
  getOrderEvents: jest.fn(),
  updateOrderStatus: jest.fn(),
  assignDriver: jest.fn(),
  cancelOrder: jest.fn(),
  holdOrder: jest.fn(),
  releaseHoldOrder: jest.fn(),
}));

jest.mock("../../services/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  },
  API_TIMEOUTS: { STANDARD: 30000 },
  ApiTimeoutError: class extends Error {},
}));

// DriverPicker (mounted in the Assign Driver modal) loads the active driver
// roster from complianceApi. Provide a deterministic roster so the picker can
// be exercised in tests.
jest.mock("../../services/complianceApi", () => ({
  getDrivers: jest.fn().mockResolvedValue({
    data: [
      {
        driver_id: "drv-off",
        full_name: "Off Duty Dave",
        cdl_class: "A",
        status: "active",
      },
    ],
    page: 1,
    size: 200,
    total: 1,
    request_id: "drivers-1",
  }),
}));

import OrderDetailPage from "../../app/orders/[orderId]/page";
import type { FuelOrder, FuelOrderEvent } from "../../services/ordersApi";
import {
  assignDriver,
  cancelOrder,
  getOrder,
  getOrderEvents,
  holdOrder,
  releaseHoldOrder,
  updateOrderStatus,
} from "../../services/ordersApi";

const mockGetOrder = getOrder as jest.MockedFunction<typeof getOrder>;
const mockGetOrderEvents = getOrderEvents as jest.MockedFunction<
  typeof getOrderEvents
>;
const mockUpdateOrderStatus = updateOrderStatus as jest.MockedFunction<
  typeof updateOrderStatus
>;
const mockAssignDriver = assignDriver as jest.MockedFunction<
  typeof assignDriver
>;
const mockCancelOrder = cancelOrder as jest.MockedFunction<typeof cancelOrder>;
const mockHoldOrder = holdOrder as jest.MockedFunction<typeof holdOrder>;
const mockReleaseHoldOrder = releaseHoldOrder as jest.MockedFunction<
  typeof releaseHoldOrder
>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function orderFixture(overrides: Partial<FuelOrder> = {}): FuelOrder {
  return {
    order_id: "ord_test123",
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

function eventFixture(overrides: Partial<FuelOrderEvent> = {}): FuelOrderEvent {
  return {
    event_id: "evt_001",
    order_id: "ord_test123",
    tenant_id: "tenant-a",
    event_type: "order_placed",
    event_payload: {},
    event_timestamp: "2024-06-01T08:00:00Z",
    ingested_at: "2024-06-01T08:00:01Z",
    source_schema_version: "1.0",
    trace_id: "trace-001",
    location: null,
    ...overrides,
  };
}

beforeEach(() => {
  mockGetOrder.mockReset();
  mockGetOrderEvents.mockReset();
  mockUpdateOrderStatus.mockReset();
  mockAssignDriver.mockReset();
  mockCancelOrder.mockReset();
  mockHoldOrder.mockReset();
  mockReleaseHoldOrder.mockReset();
});

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("OrderDetailPage — render", () => {
  it("renders order details and event timeline", async () => {
    mockGetOrder.mockResolvedValue({ data: orderFixture(), request_id: "r1" });
    mockGetOrderEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    });

    render(<OrderDetailPage />);

    expect(await screen.findByText(/acme fuel co/i)).toBeInTheDocument();
    expect(screen.getByText("DIESEL_2")).toBeInTheDocument();
    expect(screen.getByText(/order placed/i)).toBeInTheDocument();
  });

  it("renders error state when fetch fails", async () => {
    mockGetOrder.mockRejectedValue(new Error("Not found"));
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    expect(await screen.findByText(/not found/i)).toBeInTheDocument();
  });
});

describe("OrderDetailPage — intake metadata", () => {
  it("renders dispatcher metadata for dispatcher channel", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({
        intake_channel: "dispatcher",
        intake_metadata: {
          dispatcher_user_id: "disp-42",
          session_id: "sess-99",
        },
      }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    expect(await screen.findByText("disp-42")).toBeInTheDocument();
    expect(screen.getByText("sess-99")).toBeInTheDocument();
  });

  it("renders voice metadata with transcript and recording", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({
        intake_channel: "voice",
        intake_metadata: {
          call_id: "call-123",
          transcript: "I need 500 gallons of diesel",
          recording_url: "https://example.com/recording.mp3",
          agent_confidence: 0.95,
        },
      }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    expect(
      await screen.findByText(/500 gallons of diesel/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/call recording/i)).toBeInTheDocument();
    expect(screen.getByText("95%")).toBeInTheDocument();
  });

  it("renders csv metadata with import batch link", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({
        intake_channel: "csv",
        intake_metadata: { import_batch_id: "batch-777", csv_row_number: 42 },
      }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    expect(await screen.findByText("batch-777")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });
});

describe("OrderDetailPage — storm mode banner", () => {
  it("shows storm mode banner when event has storm_event_id", async () => {
    mockGetOrder.mockResolvedValue({ data: orderFixture(), request_id: "r1" });
    mockGetOrderEvents.mockResolvedValue({
      data: [eventFixture({ event_payload: { storm_event_id: "storm-001" } })],
      request_id: "r2",
    });

    render(<OrderDetailPage />);

    const banner = await screen.findByTestId("storm-mode-banner");
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent("storm-001");
  });

  it("does not show storm banner when no storm event", async () => {
    mockGetOrder.mockResolvedValue({ data: orderFixture(), request_id: "r1" });
    mockGetOrderEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    });

    render(<OrderDetailPage />);

    await waitFor(() => expect(mockGetOrder).toHaveBeenCalled());
    expect(screen.queryByTestId("storm-mode-banner")).not.toBeInTheDocument();
  });
});

describe("OrderDetailPage — assigned driver card", () => {
  it("shows driver card when assigned_driver_id is present", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({
        assigned_driver_id: "drv-007",
        assigned_run_id: "run-1",
      }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    expect(
      await screen.findByTestId("assigned-driver-card"),
    ).toBeInTheDocument();
    expect(screen.getByText("drv-007")).toBeInTheDocument();
    expect(screen.getByText(/run-1/)).toBeInTheDocument();
  });
});

describe("OrderDetailPage — POD section", () => {
  it("shows POD section when status is delivered", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({ status: "delivered" }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    expect(await screen.findByTestId("pod-section")).toBeInTheDocument();
  });

  it("does not show POD section for non-delivered orders", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({ status: "placed" }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    await waitFor(() => expect(mockGetOrder).toHaveBeenCalled());
    expect(screen.queryByTestId("pod-section")).not.toBeInTheDocument();
  });
});

describe("OrderDetailPage — cross-module resolved links", () => {
  it("renders the resolved customer name and links to the customer module", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({
        customer_name: "Stale Snapshot Name",
        links: {
          customer: {
            status: "resolved",
            id: "CUST-001",
            summary: { display_name: "Acme Fuel Co" },
          },
        },
      }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    // Resolved name is preferred over the snapshot (Req 1.4).
    const customerLink = await screen.findByRole("link", {
      name: /acme fuel co/i,
    });
    expect(customerLink).toHaveAttribute(
      "href",
      "/commerce/customers/CUST-001",
    );
    expect(screen.queryByText(/stale snapshot name/i)).not.toBeInTheDocument();
  });

  it("renders a navigable asset card and driver card from resolved links", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({
        assigned_driver_id: "drv-007",
        assigned_asset_id: "asset-123",
        links: {
          asset: {
            status: "resolved",
            id: "asset-123",
            summary: { name: "Tanker 9" },
          },
          driver: {
            status: "resolved",
            id: "drv-007",
            summary: { driver_name: "Jane Driver" },
          },
        },
      }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    expect(
      await screen.findByTestId("assigned-asset-card"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /tanker 9/i })).toHaveAttribute(
      "href",
      "/ops/tracking/asset-123",
    );
    expect(screen.getByRole("link", { name: /jane driver/i })).toHaveAttribute(
      "href",
      "/ops/drivers?driver=drv-007",
    );
  });

  it("shows an explicit Unlinked affordance for an unresolved asset reference", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({
        assigned_asset_id: "asset-gone",
        links: {
          asset: { status: "unresolved", id: "asset-gone" },
        },
      }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    const card = await screen.findByTestId("assigned-asset-card");
    expect(card).toHaveTextContent("asset-gone");
    expect(card).toHaveTextContent(/unlinked/i);
    // An unresolved reference must NOT be a dead link.
    expect(
      screen.queryByRole("link", { name: /asset-gone/i }),
    ).not.toBeInTheDocument();
  });

  it("links optimistically on the document id when links are absent", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({ assigned_driver_id: "drv-007" }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    const driverLink = await screen.findByRole("link", { name: /drv-007/i });
    expect(driverLink).toHaveAttribute("href", "/ops/drivers?driver=drv-007");
  });
});

describe("OrderDetailPage — mutation controls", () => {
  it("hides mutation buttons for terminal statuses", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({ status: "delivered" }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    await waitFor(() => expect(mockGetOrder).toHaveBeenCalled());
    expect(
      screen.queryByRole("button", { name: /change status/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /cancel order/i }),
    ).not.toBeInTheDocument();
  });

  it("shows cancel modal (HIGH risk) and submits", async () => {
    mockGetOrder.mockResolvedValue({ data: orderFixture(), request_id: "r1" });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });
    mockCancelOrder.mockResolvedValue({
      data: orderFixture({ status: "cancelled" }),
      request_id: "r3",
    });

    render(<OrderDetailPage />);

    await waitFor(() => expect(mockGetOrder).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /cancel order/i }));

    // Modal appears
    expect(
      await screen.findByText(/this action cannot be undone/i),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/cancellation reason/i), {
      target: { value: "Customer requested" },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /confirm cancel/i }));
    });

    await waitFor(() =>
      expect(mockCancelOrder).toHaveBeenCalledWith("ord_test123", {
        reason: "Customer requested",
      }),
    );
  });

  it("shows hold modal and submits a hold reason", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({ status: "placed" }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });
    mockHoldOrder.mockResolvedValue({
      data: orderFixture({ status: "on_hold", hold_reason: "credit check" }),
      request_id: "r3",
    });

    render(<OrderDetailPage />);

    expect(await screen.findByText(/acme fuel co/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^place on hold$/i }));

    // Modal appears with the reason textarea
    const reasonInput = await screen.findByLabelText(/hold reason/i);
    fireEvent.change(reasonInput, { target: { value: "credit check" } });

    await act(async () => {
      // The modal's primary submit button also reads "Place on Hold".
      const buttons = screen.getAllByRole("button", {
        name: /^place on hold$/i,
      });
      fireEvent.click(buttons[buttons.length - 1]);
    });

    await waitFor(() =>
      expect(mockHoldOrder).toHaveBeenCalledWith("ord_test123", {
        hold_reason: "credit check",
      }),
    );
  });

  it("shows release-hold action for an on_hold order and submits", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({ status: "on_hold", hold_reason: "credit check" }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });
    mockReleaseHoldOrder.mockResolvedValue({
      data: orderFixture({ status: "placed" }),
      request_id: "r3",
    });

    render(<OrderDetailPage />);

    expect(await screen.findByText(/acme fuel co/i)).toBeInTheDocument();

    // An on_hold order must NOT offer "Place on Hold" but MUST offer "Release Hold".
    expect(
      screen.queryByRole("button", { name: /^place on hold$/i }),
    ).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /release hold/i }));
    });

    await waitFor(() =>
      expect(mockReleaseHoldOrder).toHaveBeenCalledWith("ord_test123"),
    );
  });

  it("does not offer hold actions for an in_transit order", async () => {
    mockGetOrder.mockResolvedValue({
      data: orderFixture({ status: "in_transit" }),
      request_id: "r1",
    });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });

    render(<OrderDetailPage />);

    expect(await screen.findByText(/acme fuel co/i)).toBeInTheDocument();
    // in_transit is past the holdable window and not on_hold.
    expect(
      screen.queryByRole("button", { name: /^place on hold$/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /release hold/i }),
    ).not.toBeInTheDocument();
  });

  it("surfaces backend error_code on mutation failure", async () => {
    const { ApiError } = jest.requireMock("../../services/api");
    mockGetOrder.mockResolvedValue({ data: orderFixture(), request_id: "r1" });
    mockGetOrderEvents.mockResolvedValue({ data: [], request_id: "r2" });
    mockAssignDriver.mockRejectedValue(new ApiError("driver_unavailable", 409));

    render(<OrderDetailPage />);

    // Wait for the page to load
    expect(await screen.findByText(/acme fuel co/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /assign driver/i }));

    // Open the driver picker and choose the (off-duty) driver from the roster.
    fireEvent.click(await screen.findByLabelText(/^driver$/i));
    fireEvent.click(await screen.findByText("Off Duty Dave"));

    await act(async () => {
      fireEvent.click(screen.getByText("Assign"));
    });

    await waitFor(() => {
      expect(screen.getByText(/driver_unavailable/i)).toBeInTheDocument();
    });
  });
});
