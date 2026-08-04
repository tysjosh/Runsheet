/**
 * Tests for InvoiceDetailPage component.
 *
 * Covers:
 * - Happy path: invoice + events render
 * - Event timeline display
 * - Void dialog open/close/submit
 * - Force void with applied payments
 * - QBO retry button
 * - WebSocket subscription for live updates
 * - Error state rendering
 * - Loading state rendering
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

// Mock WebSocket
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 1;

  constructor() {
    MockWebSocket.instances.push(this);
  }

  close() {
    this.readyState = 3;
  }

  simulateMessage(data: unknown) {
    if (this.onmessage) {
      this.onmessage({ data: JSON.stringify(data) });
    }
  }
}

(global as any).WebSocket = MockWebSocket;

jest.mock("../../../services/commerceApi", () => ({
  getInvoice: jest.fn(),
  getInvoiceEvents: jest.fn(),
  voidInvoice: jest.fn(),
  retryQboPush: jest.fn(),
  finalizeInvoice: jest.fn(),
}));

import {
  finalizeInvoice,
  getInvoice,
  getInvoiceEvents,
  retryQboPush,
  voidInvoice,
} from "../../../services/commerceApi";
import InvoiceDetailPage from "../InvoiceDetailPage";

const mockGetInvoice = getInvoice as jest.MockedFunction<typeof getInvoice>;
const mockGetInvoiceEvents = getInvoiceEvents as jest.MockedFunction<
  typeof getInvoiceEvents
>;
const mockVoidInvoice = voidInvoice as jest.MockedFunction<typeof voidInvoice>;
const mockRetryQboPush = retryQboPush as jest.MockedFunction<
  typeof retryQboPush
>;
const mockFinalizeInvoice = finalizeInvoice as jest.MockedFunction<
  typeof finalizeInvoice
>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function invoiceFixture(overrides: Record<string, unknown> = {}) {
  return {
    invoice_id: "inv_001",
    tenant_id: "tenant-a",
    customer_id: "cust_001",
    account_id: "acc_001",
    order_id: null,
    invoice_number: "INV-2024-0001",
    status: "open",
    total_cents: 2500000,
    amount_paid_cents: 0,
    remaining_cents: 2500000,
    tax_cents: 200000,
    subtotal_cents: 2300000,
    line_items: [
      {
        line_id: "li_001",
        product_code: "ULSD",
        quantity_gallons: 1000,
        unit_price_cents: 2300,
        subtotal_cents: 2300000,
      },
    ],
    issued_at: "2024-06-01T10:00:00Z",
    due_date: "2024-07-01",
    finalized_at: "2024-06-01T10:00:00Z",
    voided_at: null,
    void_reason: null,
    qbo_push_state: "pushed",
    qbo_push_attempts: 1,
    qbo_push_last_error: null,
    external_refs: { qbo: "QBO-123" },
    created_at: "2024-06-01T10:00:00Z",
    updated_at: "2024-06-01T10:00:00Z",
    ...overrides,
  };
}

function eventFixture(overrides: Record<string, unknown> = {}) {
  return {
    event_id: "evt_001",
    invoice_id: "inv_001",
    tenant_id: "tenant-a",
    event_type: "created",
    actor: "system",
    occurred_at: "2024-06-01T10:00:00Z",
    payload: {},
    ...overrides,
  };
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("InvoiceDetailPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    MockWebSocket.instances = [];
  });

  it("renders invoice details and event timeline", async () => {
    mockGetInvoice.mockResolvedValue({
      data: invoiceFixture(),
      request_id: "r1",
    } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [
        eventFixture(),
        eventFixture({ event_id: "evt_002", event_type: "finalized" }),
      ],
      request_id: "r2",
    } as any);

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    await waitFor(() => {
      expect(screen.getByText("Invoice INV-2024-0001")).toBeInTheDocument();
    });

    // Summary values — total and remaining are both $25,000.00 since no payments
    expect(screen.getAllByText("$25,000.00").length).toBe(2); // total + remaining
    // Subtotal appears in summary card and line items table
    expect(screen.getAllByText("$23,000.00").length).toBeGreaterThanOrEqual(1);

    // Line items
    expect(screen.getByText("ULSD")).toBeInTheDocument();

    // Event timeline
    expect(screen.getByLabelText("Invoice events")).toBeInTheDocument();
    expect(screen.getByText("created")).toBeInTheDocument();
    expect(screen.getByText("finalized")).toBeInTheDocument();
  });

  it("shows loading state initially", () => {
    mockGetInvoice.mockReturnValue(new Promise(() => {}));
    mockGetInvoiceEvents.mockReturnValue(new Promise(() => {}));

    render(<InvoiceDetailPage invoiceId="inv_001" />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("shows error state on fetch failure", async () => {
    mockGetInvoice.mockRejectedValue(new Error("Not found"));
    mockGetInvoiceEvents.mockRejectedValue(new Error("Not found"));

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/Not found/)).toBeInTheDocument();
    });
  });

  it("opens void dialog and submits void request", async () => {
    const voidedInvoice = invoiceFixture({
      status: "void",
      voided_at: "2024-06-15T10:00:00Z",
    });
    mockGetInvoice.mockResolvedValue({
      data: invoiceFixture(),
      request_id: "r1",
    } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    } as any);
    mockVoidInvoice.mockResolvedValue({
      data: voidedInvoice,
      request_id: "r3",
    } as any);

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    await waitFor(() => {
      expect(screen.getByText("Invoice INV-2024-0001")).toBeInTheDocument();
    });

    // Open void dialog
    fireEvent.click(screen.getByRole("button", { name: /Void Invoice/i }));

    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });

    // Fill reason
    const reasonInput = screen.getByLabelText("Reason");
    fireEvent.change(reasonInput, { target: { value: "Duplicate invoice" } });

    // Submit
    fireEvent.click(screen.getByRole("button", { name: /Confirm Void/i }));

    await waitFor(() => {
      expect(mockVoidInvoice).toHaveBeenCalledWith("inv_001", {
        reason: "Duplicate invoice",
        force: false,
      });
    });
  });

  it("shows force void checkbox when payments are applied", async () => {
    const invoiceWithPayments = invoiceFixture({
      amount_paid_cents: 1000000,
      remaining_cents: 1500000,
    });
    mockGetInvoice.mockResolvedValue({
      data: invoiceWithPayments,
      request_id: "r1",
    } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    } as any);

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    await waitFor(() => {
      expect(screen.getByText("Invoice INV-2024-0001")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Void Invoice/i }));

    await waitFor(() => {
      expect(screen.getByText(/Force void/)).toBeInTheDocument();
      expect(
        screen.getByText(/\$10,000.00 in applied payments/),
      ).toBeInTheDocument();
    });
  });

  it("retries QBO push for dead-lettered invoices", async () => {
    const deadLetterInvoice = invoiceFixture({ qbo_push_state: "dead_letter" });
    const retriedInvoice = invoiceFixture({ qbo_push_state: "pending" });
    mockGetInvoice.mockResolvedValue({
      data: deadLetterInvoice,
      request_id: "r1",
    } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    } as any);
    mockRetryQboPush.mockResolvedValue({
      data: retriedInvoice,
      request_id: "r3",
    } as any);

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    await waitFor(() => {
      expect(screen.getByText("dead_letter")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Retry Push/i }));

    await waitFor(() => {
      expect(mockRetryQboPush).toHaveBeenCalledWith("inv_001");
    });
  });

  it("approves a draft invoice and starts ERP export", async () => {
    const draft = invoiceFixture({
      status: "draft",
      invoice_number: null,
      issued_at: null,
      finalized_at: null,
    });
    const finalized = invoiceFixture({ status: "open" });
    mockGetInvoice.mockResolvedValue({ data: draft, request_id: "r1" } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    } as any);
    mockFinalizeInvoice.mockResolvedValue({
      data: finalized,
      request_id: "r3",
    } as any);

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    const approve = await screen.findByRole("button", {
      name: /Approve & Send to ERP/i,
    });
    fireEvent.click(approve);

    await waitFor(() => {
      expect(mockFinalizeInvoice).toHaveBeenCalledWith("inv_001");
      expect(screen.getByText("open")).toBeInTheDocument();
    });
  });

  it("shows the POD delivery result used for billing", async () => {
    mockGetInvoice.mockResolvedValue({
      data: invoiceFixture({
        pod_id: "pod-42",
        delivered_at: "2024-06-01T09:45:00Z",
        delivery_result: {
          pod_id: "pod-42",
          actual_gallons: 975.25,
          actual_gallons_source: "manual",
          recipient_name: "Alex Receiver",
          delivered_at: "2024-06-01T09:45:00Z",
        },
      }),
      request_id: "r1",
    } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    } as any);

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    expect(await screen.findByText("Delivery Result")).toBeInTheDocument();
    expect(screen.getByText("975.25 gal")).toBeInTheDocument();
    expect(screen.getByText("Alex Receiver")).toBeInTheDocument();
    expect(screen.getByText("pod-42")).toBeInTheDocument();
  });

  it("receives live updates via WebSocket", async () => {
    mockGetInvoice.mockResolvedValue({
      data: invoiceFixture(),
      request_id: "r1",
    } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    } as any);

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    await waitFor(() => {
      expect(screen.getByText("Invoice INV-2024-0001")).toBeInTheDocument();
    });

    // Simulate WebSocket message
    const ws = MockWebSocket.instances[0];
    expect(ws).toBeDefined();

    act(() => {
      ws.simulateMessage({
        type: "invoice_event",
        data: eventFixture({
          event_id: "evt_003",
          event_type: "payment_applied",
          occurred_at: "2024-06-15T10:00:00Z",
          actor: "stripe",
        }),
      });
    });

    await waitFor(() => {
      expect(screen.getByText("payment applied")).toBeInTheDocument();
    });
  });

  it("does not show void button for already voided invoices", async () => {
    const voidedInvoice = invoiceFixture({ status: "void" });
    mockGetInvoice.mockResolvedValue({
      data: voidedInvoice,
      request_id: "r1",
    } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    } as any);

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    await waitFor(() => {
      expect(screen.getByText("Invoice INV-2024-0001")).toBeInTheDocument();
    });

    expect(
      screen.queryByRole("button", { name: /Void Invoice/i }),
    ).not.toBeInTheDocument();
  });

  it("links the customer reference to the canonical customer route", async () => {
    mockGetInvoice.mockResolvedValue({
      data: invoiceFixture(),
      request_id: "r1",
    } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    } as any);

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    await waitFor(() => {
      expect(screen.getByText("Invoice INV-2024-0001")).toBeInTheDocument();
    });

    // Customer reference is navigable to its owning module (Req 12.1, 13.1).
    const customerLink = screen.getByRole("link", { name: /cust_001/ });
    expect(customerLink).toHaveAttribute(
      "href",
      "/commerce/customers/cust_001",
    );
  });

  it("links a present order reference to the orders route", async () => {
    mockGetInvoice.mockResolvedValue({
      data: invoiceFixture({ order_id: "ord_777" }),
      request_id: "r1",
    } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    } as any);

    render(<InvoiceDetailPage invoiceId="inv_001" />);

    await waitFor(() => {
      expect(screen.getByText("Invoice INV-2024-0001")).toBeInTheDocument();
    });

    const orderLink = screen.getByRole("link", { name: /ord_777/ });
    expect(orderLink).toHaveAttribute("href", "/orders/ord_777");
  });

  it("navigates to the account in-hub via the onViewAccount callback", async () => {
    mockGetInvoice.mockResolvedValue({
      data: invoiceFixture(),
      request_id: "r1",
    } as any);
    mockGetInvoiceEvents.mockResolvedValue({
      data: [eventFixture()],
      request_id: "r2",
    } as any);

    const onViewAccount = jest.fn();
    render(
      <InvoiceDetailPage invoiceId="inv_001" onViewAccount={onViewAccount} />,
    );

    await waitFor(() => {
      expect(screen.getByText("Invoice INV-2024-0001")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /acc_001/ }));
    expect(onViewAccount).toHaveBeenCalledWith("acc_001");
  });
});
