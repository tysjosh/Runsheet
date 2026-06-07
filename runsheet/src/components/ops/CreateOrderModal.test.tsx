/**
 * Tests for CreateOrderModal component.
 *
 * Covers:
 * - Happy path: valid form submission
 * - Validation errors: missing_volume, invalid_delivery_window
 * - will_call allows null window
 * - fill_to_full bypasses gallons validation
 * - API error surfacing
 * - Modal open/close behavior
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

jest.mock("../../services/ordersApi", () => ({
  createOrder: jest.fn(),
}));

jest.mock("../../services/commerceApi", () => ({
  getCustomers: jest.fn(),
}));

jest.mock("../../services/fuelApi", () => ({
  listFuelProducts: jest.fn(),
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

import { getCustomers } from "../../services/commerceApi";
import { listFuelProducts } from "../../services/fuelApi";
import { createOrder } from "../../services/ordersApi";
import CreateOrderModal, {
  type CreateOrderFormValues,
  validateCreateOrderForm,
} from "./CreateOrderModal";

const mockCreateOrder = createOrder as jest.MockedFunction<typeof createOrder>;
const mockGetCustomers = getCustomers as jest.MockedFunction<
  typeof getCustomers
>;
const mockListFuelProducts = listFuelProducts as jest.MockedFunction<
  typeof listFuelProducts
>;

// ─── Picker helper ───────────────────────────────────────────────────────────

/**
 * Drive a SearchableSelect-backed picker: open it by its trigger button's
 * accessible name, then click the option whose label matches `optionName`.
 * Options load asynchronously on mount, so `findByRole` polls until ready.
 */
async function pickOption(triggerName: string, optionName: RegExp) {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: triggerName }));
  });
  const option = await screen.findByRole("option", { name: optionName });
  await act(async () => {
    fireEvent.click(option);
  });
}

// ─── Fixtures ────────────────────────────────────────────────────────────────

function validForm(
  overrides: Partial<CreateOrderFormValues> = {},
): CreateOrderFormValues {
  return {
    customer_id: "CUST-001",
    customer_name: "Acme Fuel",
    customer_phone: "555-0100",
    customer_email: "acme@example.com",
    ship_to_address: "123 Main St",
    ship_to_lat: "40.7128",
    ship_to_lon: "-74.006",
    customer_tank_id: "",
    product_code: "DIESEL_2",
    gallons_requested: "500",
    fill_to_full: false,
    call_type: "one_off",
    delivery_window_start: "2024-06-01T08:00",
    delivery_window_end: "2024-06-01T17:00",
    po_number: "",
    special_instructions: "",
    ...overrides,
  };
}

beforeEach(() => {
  mockCreateOrder.mockReset();
  mockGetCustomers.mockReset();
  mockListFuelProducts.mockReset();

  // Roster backing CustomerPicker — includes the id the tests select.
  mockGetCustomers.mockResolvedValue({
    data: [
      {
        customer_id: "CUST-001",
        display_name: "Acme Fuel",
        status: "active",
      } as never,
    ],
    cursor: null,
    has_more: false,
    request_id: "req-customers",
  });

  // Catalog backing ProductPicker — includes the code the tests select.
  mockListFuelProducts.mockResolvedValue({
    region: "US",
    items: [
      {
        product_code: "DIESEL_2",
        display_name: "Diesel #2",
        category: "diesel",
      } as never,
    ],
    total: 1,
  });
});

// ─── Pure validation tests ───────────────────────────────────────────────────

describe("validateCreateOrderForm", () => {
  it("returns no errors for a valid form", () => {
    expect(validateCreateOrderForm(validForm())).toEqual({});
  });

  it("flags missing customer_id", () => {
    const errors = validateCreateOrderForm(validForm({ customer_id: "" }));
    expect(errors.customer_id).toMatch(/required/i);
  });

  it("flags missing product_code", () => {
    const errors = validateCreateOrderForm(validForm({ product_code: "" }));
    expect(errors.product_code).toMatch(/required/i);
  });

  it("flags missing_volume when gallons <= 0 and not fill_to_full", () => {
    const errors = validateCreateOrderForm(
      validForm({ gallons_requested: "0", fill_to_full: false }),
    );
    expect(errors.gallons_requested).toMatch(/greater than 0/i);
  });

  it("accepts null gallons when fill_to_full is true", () => {
    const errors = validateCreateOrderForm(
      validForm({ gallons_requested: "", fill_to_full: true }),
    );
    expect(errors.gallons_requested).toBeUndefined();
  });

  it("flags invalid_delivery_window when end <= start", () => {
    const errors = validateCreateOrderForm(
      validForm({
        delivery_window_start: "2024-06-01T17:00",
        delivery_window_end: "2024-06-01T08:00",
      }),
    );
    expect(errors.delivery_window_end).toMatch(/after start/i);
  });

  it("requires window for one_off call_type", () => {
    const errors = validateCreateOrderForm(
      validForm({
        call_type: "one_off",
        delivery_window_start: "",
        delivery_window_end: "",
      }),
    );
    expect(errors.delivery_window_start).toMatch(/required/i);
    expect(errors.delivery_window_end).toMatch(/required/i);
  });

  it("allows null window for will_call", () => {
    const errors = validateCreateOrderForm(
      validForm({
        call_type: "will_call",
        delivery_window_start: "",
        delivery_window_end: "",
      }),
    );
    expect(errors.delivery_window_start).toBeUndefined();
    expect(errors.delivery_window_end).toBeUndefined();
  });

  it("allows null window for auto_fill", () => {
    const errors = validateCreateOrderForm(
      validForm({
        call_type: "auto_fill",
        delivery_window_start: "",
        delivery_window_end: "",
      }),
    );
    expect(errors.delivery_window_start).toBeUndefined();
  });

  it("allows null window for keep_full", () => {
    const errors = validateCreateOrderForm(
      validForm({
        call_type: "keep_full",
        delivery_window_start: "",
        delivery_window_end: "",
      }),
    );
    expect(errors.delivery_window_start).toBeUndefined();
  });

  it("flags out-of-range latitude", () => {
    const errors = validateCreateOrderForm(validForm({ ship_to_lat: "91" }));
    expect(errors.ship_to_lat).toMatch(/-90 and 90/);
  });

  it("flags out-of-range longitude", () => {
    const errors = validateCreateOrderForm(validForm({ ship_to_lon: "-181" }));
    expect(errors.ship_to_lon).toMatch(/-180 and 180/);
  });
});

// ─── Component tests ─────────────────────────────────────────────────────────

describe("CreateOrderModal — render", () => {
  it("does not render when isOpen is false", () => {
    render(<CreateOrderModal isOpen={false} onClose={jest.fn()} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders the modal when isOpen is true", () => {
    render(<CreateOrderModal isOpen={true} onClose={jest.fn()} />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /create order/i }),
    ).toBeInTheDocument();
  });
});

describe("CreateOrderModal — submission", () => {
  it("blocks submission when required fields are missing", async () => {
    render(<CreateOrderModal isOpen={true} onClose={jest.fn()} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /submit order/i }));
    });

    expect(mockCreateOrder).not.toHaveBeenCalled();
    expect(screen.getByText(/customer id is required/i)).toBeInTheDocument();
  });

  it("calls createOrder with valid payload on successful submission", async () => {
    mockCreateOrder.mockResolvedValue({
      event_id: "evt_1",
      status: "accepted",
      order_id: "ord_new123",
    });
    const onSuccess = jest.fn();
    const onClose = jest.fn();

    render(
      <CreateOrderModal
        isOpen={true}
        onClose={onClose}
        onSuccess={onSuccess}
        dispatcherUserId="user-1"
      />,
    );

    // Fill required fields using label associations
    const getInput = (id: string) =>
      document.getElementById(id) as HTMLInputElement;

    // Customer ID and Product Code are now searchable pickers — select rather
    // than type. The picker returns the underlying id/code as the value.
    await pickOption("Customer ID", /Acme Fuel/);
    await pickOption("Product Code", /Diesel #2/);

    await act(async () => {
      fireEvent.change(getInput("co-customer-name"), {
        target: { value: "Acme" },
      });
      fireEvent.change(getInput("co-address"), {
        target: { value: "123 Main" },
      });
      fireEvent.change(getInput("co-lat"), { target: { value: "40.7" } });
      fireEvent.change(getInput("co-lon"), { target: { value: "-74.0" } });
      fireEvent.change(getInput("co-gallons"), { target: { value: "500" } });
      fireEvent.change(getInput("co-window-start"), {
        target: { value: "2024-06-01T08:00" },
      });
      fireEvent.change(getInput("co-window-end"), {
        target: { value: "2024-06-01T17:00" },
      });
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /submit order/i }));
    });

    await waitFor(() => expect(mockCreateOrder).toHaveBeenCalledTimes(1));
    const payload = mockCreateOrder.mock.calls[0][0];
    expect(payload.customer_id).toBe("CUST-001");
    expect(payload.product_code).toBe("DIESEL_2");
    expect(payload.gallons_requested).toBe(500);
    expect(payload.client_event_id).toBeDefined();

    expect(onSuccess).toHaveBeenCalledWith("ord_new123");
    expect(onClose).toHaveBeenCalled();
  });

  it("surfaces API error in the form", async () => {
    const { ApiError } = jest.requireMock("../../services/api");
    mockCreateOrder.mockRejectedValue(new ApiError("missing_volume", 400));

    render(<CreateOrderModal isOpen={true} onClose={jest.fn()} />);

    const getInput = (id: string) =>
      document.getElementById(id) as HTMLInputElement;

    await pickOption("Customer ID", /Acme Fuel/);
    await pickOption("Product Code", /Diesel #2/);

    await act(async () => {
      fireEvent.change(getInput("co-customer-name"), {
        target: { value: "Acme" },
      });
      fireEvent.change(getInput("co-address"), {
        target: { value: "123 Main" },
      });
      fireEvent.change(getInput("co-lat"), { target: { value: "40.7" } });
      fireEvent.change(getInput("co-lon"), { target: { value: "-74.0" } });
      fireEvent.change(getInput("co-gallons"), { target: { value: "500" } });
      fireEvent.change(getInput("co-window-start"), {
        target: { value: "2024-06-01T08:00" },
      });
      fireEvent.change(getInput("co-window-end"), {
        target: { value: "2024-06-01T17:00" },
      });
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /submit order/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/missing_volume/i)).toBeInTheDocument();
    });
  });
});
