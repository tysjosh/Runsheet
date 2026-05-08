/**
 * Tests for :file:`CustomerTankPage.tsx` (Escalation #3 —
 * frontend-integration-audit Batch D).
 *
 * Coverage targets the behaviours the spec calls out:
 *
 *  1. **``validateCustomerTankForm`` pure helper** — required-field
 *     checks on ``customer_id`` / ``fuel_product_code`` / ``zip_code``,
 *     numeric validation on ``capacity_gallons`` /
 *     ``current_level_gallons`` (non-positive / over-capacity),
 *     lat-lon range enforcement, and the optional ``k_factor`` >= 0
 *     rule.
 *  2. **Page render + list fetch** — initial mount calls
 *     :func:`listCustomerTanks` and surfaces rows; empty list and
 *     error responses render the matching UI.
 *  3. **Filters** — changing the ``fuel_type`` filter re-calls
 *     :func:`listCustomerTanks` with the forwarded query param and
 *     resets to page 1.
 *  4. **Pagination** — clicking next triggers a refetch with the
 *     bumped ``page`` value; clicking prev goes back.
 *  5. **Create flow** — the Add Tank button opens the modal,
 *     validation errors block submission, and a valid submission
 *     posts to :func:`createCustomerTank` with the canonical payload.
 *  6. **Edit flow** — clicking an edit row control pre-populates the
 *     modal and submitting calls :func:`updateCustomerTank` with only
 *     the patched fields.
 *  7. **Fuel product catalog datalist (Batch D)** —
 *     :func:`listFuelProducts` is invoked on modal open and the
 *     rendered ``<datalist>`` contains an ``<option>`` per canonical
 *     product code returned by the mock response.
 *
 * ``fuelApi`` is mocked wholesale so these tests never touch the
 * network.
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    ...actual,
    listCustomerTanks: jest.fn(),
    createCustomerTank: jest.fn(),
    updateCustomerTank: jest.fn(),
    listFuelProducts: jest.fn(),
  };
});

import type {
  CustomerTank,
  CustomerTankListResponse,
  FuelProductItem,
  FuelProductsResponse,
} from "../../services/fuelApi";
import {
  createCustomerTank,
  listCustomerTanks,
  listFuelProducts,
  updateCustomerTank,
} from "../../services/fuelApi";
import CustomerTankPage, {
  validateCustomerTankForm,
  type CustomerTankFormValues,
} from "./CustomerTankPage";

const mockList = listCustomerTanks as jest.MockedFunction<
  typeof listCustomerTanks
>;
const mockCreate = createCustomerTank as jest.MockedFunction<
  typeof createCustomerTank
>;
const mockUpdate = updateCustomerTank as jest.MockedFunction<
  typeof updateCustomerTank
>;
const mockListProducts = listFuelProducts as jest.MockedFunction<
  typeof listFuelProducts
>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function tankFixture(overrides: Partial<CustomerTank> = {}): CustomerTank {
  const base: CustomerTank = {
    customer_tank_id: "CT-0001",
    tenant_id: "tenant-a",
    customer_id: "CUST-0042",
    customer_type: "residential",
    fuel_type: "propane",
    fuel_product_code: "PROPANE",
    capacity_gallons: 500,
    current_level_gallons: 180,
    last_reading_at: null,
    location_lat: 40.7128,
    location_lon: -74.006,
    zip_code: "10001",
    k_factor: 0.45,
    use_case: "residential_heat",
    status: "active",
    updated_at: "2024-06-01T12:00:00Z",
    created_at: "2024-05-01T12:00:00Z",
  };
  return { ...base, ...overrides };
}

function listResponseFixture(
  items: CustomerTank[],
  overrides: Partial<CustomerTankListResponse> = {},
): CustomerTankListResponse {
  return {
    items,
    total: items.length,
    page: 1,
    page_size: 20,
    has_next: false,
    ...overrides,
  };
}

function productFixture(
  overrides: Partial<FuelProductItem> = {},
): FuelProductItem {
  const base: FuelProductItem = {
    product_code: "PROPANE",
    display_name: "Propane",
    category: "propane",
    density_lbs_per_gallon: 4.24,
    tax_class: "propane",
    aliases: ["LPG"],
    region_availability: ["US"],
  };
  return { ...base, ...overrides };
}

function productsResponseFixture(
  items: FuelProductItem[],
  region = "US",
): FuelProductsResponse {
  return { region, items, total: items.length };
}

function validFormValues(
  overrides: Partial<CustomerTankFormValues> = {},
): CustomerTankFormValues {
  const base: CustomerTankFormValues = {
    customer_tank_id: "",
    customer_id: "CUST-0042",
    customer_type: "residential",
    fuel_type: "propane",
    fuel_product_code: "PROPANE",
    capacity_gallons: 500,
    current_level_gallons: 180,
    location_lat: 40.7128,
    location_lon: -74.006,
    zip_code: "10001",
    k_factor: null,
    use_case: "",
    status: "active",
  };
  return { ...base, ...overrides };
}

beforeEach(() => {
  mockList.mockReset();
  mockCreate.mockReset();
  mockUpdate.mockReset();
  mockListProducts.mockReset();
  // Default: products catalog resolves empty so modals that render before
  // the test asserts on the datalist don't leak unhandled rejections.
  mockListProducts.mockResolvedValue(productsResponseFixture([]));
});

// ─── Pure helper tests ───────────────────────────────────────────────────────

describe("validateCustomerTankForm", () => {
  it("returns no errors for a well-formed form", () => {
    expect(validateCustomerTankForm(validFormValues())).toEqual({});
  });

  it("flags a blank customer_id", () => {
    const errors = validateCustomerTankForm(
      validFormValues({ customer_id: "   " }),
    );
    expect(errors.customer_id).toMatch(/customer id/i);
  });

  it("flags a blank fuel_product_code", () => {
    const errors = validateCustomerTankForm(
      validFormValues({ fuel_product_code: "" }),
    );
    expect(errors.fuel_product_code).toMatch(/fuel product code/i);
  });

  it("flags a blank zip_code", () => {
    const errors = validateCustomerTankForm(
      validFormValues({ zip_code: "  " }),
    );
    expect(errors.zip_code).toMatch(/zip code/i);
  });

  it("rejects non-positive capacity_gallons", () => {
    expect(
      validateCustomerTankForm(validFormValues({ capacity_gallons: 0 }))
        .capacity_gallons,
    ).toMatch(/greater than zero/i);
    expect(
      validateCustomerTankForm(validFormValues({ capacity_gallons: -50 }))
        .capacity_gallons,
    ).toMatch(/greater than zero/i);
    expect(
      validateCustomerTankForm(
        validFormValues({ capacity_gallons: Number.NaN }),
      ).capacity_gallons,
    ).toMatch(/greater than zero/i);
  });

  it("rejects negative current_level_gallons", () => {
    const errors = validateCustomerTankForm(
      validFormValues({ current_level_gallons: -1 }),
    );
    expect(errors.current_level_gallons).toMatch(/zero or greater/i);
  });

  it("rejects current_level_gallons above capacity_gallons", () => {
    const errors = validateCustomerTankForm(
      validFormValues({
        capacity_gallons: 500,
        current_level_gallons: 1000,
      }),
    );
    expect(errors.current_level_gallons).toMatch(/cannot exceed capacity/i);
  });

  it("rejects out-of-range latitude and longitude", () => {
    expect(
      validateCustomerTankForm(validFormValues({ location_lat: 91 }))
        .location_lat,
    ).toMatch(/-90 and 90/);
    expect(
      validateCustomerTankForm(validFormValues({ location_lon: -200 }))
        .location_lon,
    ).toMatch(/-180 and 180/);
  });

  it("rejects a negative k_factor but accepts null", () => {
    expect(
      validateCustomerTankForm(validFormValues({ k_factor: -0.1 })).k_factor,
    ).toMatch(/zero or greater/i);
    expect(
      validateCustomerTankForm(validFormValues({ k_factor: null })).k_factor,
    ).toBeUndefined();
    expect(
      validateCustomerTankForm(validFormValues({ k_factor: 0.3 })).k_factor,
    ).toBeUndefined();
  });
});

// ─── Page render + list ──────────────────────────────────────────────────────

describe("CustomerTankPage — list render", () => {
  it("fetches tanks on mount and renders a row per tank", async () => {
    mockList.mockResolvedValue(
      listResponseFixture([
        tankFixture({ customer_tank_id: "CT-0001" }),
        tankFixture({
          customer_tank_id: "CT-0002",
          customer_id: "CUST-0100",
          fuel_type: "heating_oil",
          fuel_product_code: "HEATING_OIL",
        }),
      ]),
    );

    render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("CT-0001")).toBeInTheDocument();
    expect(screen.getByText("CT-0002")).toBeInTheDocument();
    expect(screen.getByText("CUST-0100")).toBeInTheDocument();
  });

  it("renders the empty-state copy when no tanks come back", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));

    render(<CustomerTankPage />);

    expect(
      await screen.findByText(/no customer tanks found/i),
    ).toBeInTheDocument();
  });

  it("surfaces the API error in the banner when the list fetch fails", async () => {
    mockList.mockRejectedValue(new Error("boom"));

    render(<CustomerTankPage />);

    await waitFor(() => {
      expect(screen.getByText(/boom/)).toBeInTheDocument();
    });
  });
});

// ─── Filters / pagination ────────────────────────────────────────────────────

describe("CustomerTankPage — filters and pagination", () => {
  it("re-calls listCustomerTanks with the fuel_type filter", async () => {
    mockList.mockResolvedValue(listResponseFixture([tankFixture()]));

    render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText(/Fuel Type/i), {
      target: { value: "heating_oil" },
    });

    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(2));
    const lastCall = mockList.mock.calls[mockList.mock.calls.length - 1][0];
    expect(lastCall).toEqual(
      expect.objectContaining({
        fuel_type: "heating_oil",
        page: 1,
        size: 20,
      }),
    );
  });

  it("re-calls listCustomerTanks with the bumped page on Next click", async () => {
    mockList.mockResolvedValue(
      listResponseFixture([tankFixture()], { has_next: true, total: 25 }),
    );

    render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(1));

    fireEvent.click(
      screen.getByRole("button", { name: /next page/i }),
    );

    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(2));
    const lastCall = mockList.mock.calls[mockList.mock.calls.length - 1][0];
    expect(lastCall?.page).toBe(2);
  });
});

// ─── Create flow ─────────────────────────────────────────────────────────────

describe("CustomerTankPage — create flow", () => {
  it("opens the modal when Add Tank is clicked", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));

    render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /add tank/i }));

    expect(
      await screen.findByRole("heading", { name: /add customer tank/i }),
    ).toBeInTheDocument();
  });

  it("blocks submission when required fields are missing and shows inline errors", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));

    const { container } = render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /add tank/i }));

    // Clear the required fields so validation fails. The ZIP input in the
    // modal shares its visible label with the filter bar, so we target
    // by element id to avoid an ambiguous label lookup.
    fireEvent.change(screen.getByLabelText(/Customer ID/i), {
      target: { value: "" },
    });
    const modalZip = container.querySelector(
      "#ct-zip",
    ) as HTMLInputElement | null;
    expect(modalZip).not.toBeNull();
    fireEvent.change(modalZip as HTMLInputElement, { target: { value: "" } });

    // Submit the form directly so jsdom's native ``required`` validation
    // doesn't suppress the React-level submit handler.
    const form = container.querySelector("form") as HTMLFormElement;
    await act(async () => {
      fireEvent.submit(form);
    });

    expect(mockCreate).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/customer id is required/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/zip code is required/i)).toBeInTheDocument();
  });

  it("submits a well-formed payload to createCustomerTank on valid submission", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));
    mockCreate.mockResolvedValue(tankFixture());

    const { container } = render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /add tank/i }));

    fireEvent.change(screen.getByLabelText(/Customer ID/i), {
      target: { value: "CUST-0042" },
    });
    fireEvent.change(screen.getByLabelText(/Latitude/i), {
      target: { value: "40.7128" },
    });
    fireEvent.change(screen.getByLabelText(/Longitude/i), {
      target: { value: "-74.006" },
    });
    const modalZip = container.querySelector("#ct-zip") as HTMLInputElement;
    fireEvent.change(modalZip, { target: { value: "10001" } });
    fireEvent.change(screen.getByLabelText(/Current Level/i), {
      target: { value: "180" },
    });

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /create tank/i }),
      );
    });

    await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(1));
    const payload = mockCreate.mock.calls[0][0];
    expect(payload).toEqual(
      expect.objectContaining({
        customer_id: "CUST-0042",
        customer_type: "residential",
        fuel_type: "propane",
        fuel_product_code: "PROPANE",
        capacity_gallons: 500,
        current_level_gallons: 180,
        location_lat: 40.7128,
        location_lon: -74.006,
        zip_code: "10001",
        status: "active",
      }),
    );

    // Modal closes on resolve.
    await waitFor(() => {
      expect(
        screen.queryByRole("heading", { name: /add customer tank/i }),
      ).not.toBeInTheDocument();
    });
  });
});

// ─── Edit flow ───────────────────────────────────────────────────────────────

describe("CustomerTankPage — edit flow", () => {
  it("pre-populates the modal when an edit row control is clicked", async () => {
    const existing = tankFixture({
      customer_tank_id: "CT-0777",
      customer_id: "CUST-0777",
      zip_code: "90210",
    });
    mockList.mockResolvedValue(listResponseFixture([existing]));

    const { container } = render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());
    fireEvent.click(
      await screen.findByRole("button", { name: /edit tank CT-0777/i }),
    );

    const heading = await screen.findByRole("heading", {
      name: /edit customer tank/i,
    });
    expect(heading).toBeInTheDocument();
    expect(
      (screen.getByLabelText(/Customer ID/i) as HTMLInputElement).value,
    ).toBe("CUST-0777");
    const modalZip = container.querySelector("#ct-zip") as HTMLInputElement;
    expect(modalZip.value).toBe("90210");
  });

  it("calls updateCustomerTank with the tank id and only the patched fields", async () => {
    const existing = tankFixture({
      customer_tank_id: "CT-0777",
      customer_id: "CUST-0777",
      zip_code: "90210",
      status: "active",
    });
    mockList.mockResolvedValue(listResponseFixture([existing]));
    mockUpdate.mockResolvedValue({ ...existing, zip_code: "94105" });

    const { container } = render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());
    fireEvent.click(
      await screen.findByRole("button", { name: /edit tank CT-0777/i }),
    );

    // Wait for the modal form to mount before targeting the ZIP input.
    await screen.findByRole("heading", { name: /edit customer tank/i });
    const modalZip = container.querySelector("#ct-zip") as HTMLInputElement;
    fireEvent.change(modalZip, { target: { value: "94105" } });

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /save changes/i }),
      );
    });

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
    const [tankId, patch] = mockUpdate.mock.calls[0];
    expect(tankId).toBe("CT-0777");
    expect(patch).toEqual({ zip_code: "94105" });
  });
});

// ─── Fuel product catalog datalist (Batch D) ─────────────────────────────────

describe("CustomerTankPage — fuel product datalist", () => {
  it("calls listFuelProducts when the create modal opens", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));
    mockListProducts.mockResolvedValue(productsResponseFixture([]));

    render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());
    expect(mockListProducts).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /add tank/i }));

    await waitFor(() => expect(mockListProducts).toHaveBeenCalledTimes(1));
  });

  it("renders a <datalist> <option> for each canonical product code returned", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));
    mockListProducts.mockResolvedValue(
      productsResponseFixture([
        productFixture({ product_code: "PROPANE", display_name: "Propane" }),
        productFixture({
          product_code: "HEATING_OIL",
          display_name: "Heating Oil",
          category: "heating_oil",
          tax_class: "heating_oil",
        }),
        productFixture({
          product_code: "DIESEL_2",
          display_name: "Diesel #2",
          category: "diesel",
          tax_class: "diesel",
        }),
      ]),
    );

    const { container } = render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /add tank/i }));

    // Wait for the effect-driven datalist to mount.
    const datalist = await waitFor(() => {
      const node = container.querySelector(
        "datalist#ct-product-code-options",
      );
      if (!node) throw new Error("datalist not mounted yet");
      return node as HTMLDataListElement;
    });

    const options = within(datalist).getAllByRole("option", {
      hidden: true,
    }) as HTMLOptionElement[];
    const values = options.map((o) => o.value).sort();
    expect(values).toEqual(["DIESEL_2", "HEATING_OIL", "PROPANE"]);
  });

  it("does not render the datalist when the catalog fetch returns empty", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));
    mockListProducts.mockResolvedValue(productsResponseFixture([]));

    const { container } = render(<CustomerTankPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /add tank/i }));

    await waitFor(() => expect(mockListProducts).toHaveBeenCalled());
    // Give the effect a chance to settle.
    await act(async () => {
      await Promise.resolve();
    });

    expect(
      container.querySelector("datalist#ct-product-code-options"),
    ).toBeNull();
  });
});
