/**
 * Tests for DriverUtilizationList component.
 *
 * Covers:
 * - Happy path: renders driver rows
 * - Column headers show driver fields (active_order_count, completed_today)
 * - Medical card expiry warning
 * - Status filter
 * - Sorting
 * - Empty state
 */

import { fireEvent, render, screen } from "@testing-library/react";
import DriverUtilizationList, {
  type DriverUtilization,
} from "./DriverUtilizationList";

// ─── Fixtures ────────────────────────────────────────────────────────────────

function driverFixture(
  overrides: Partial<DriverUtilization> = {},
): DriverUtilization {
  return {
    driver_id: "drv-001",
    driver_name: "John Smith",
    status: "active",
    active_order_count: 3,
    completed_today: 5,
    last_seen: "2024-06-01T14:30:00Z",
    medical_card_expiry: "2025-06-01T00:00:00Z",
    assigned_truck_id: "TRK-100",
    cdl_class: "A",
    hazmat_endorsement: true,
    utilization_percentage: null,
    ...overrides,
  };
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("DriverUtilizationList — render", () => {
  it("renders the page title as Driver Utilization", () => {
    render(<DriverUtilizationList drivers={[driverFixture()]} />);
    expect(screen.getByText("Driver Utilization")).toBeInTheDocument();
  });

  it("renders driver rows with correct fields", () => {
    render(
      <DriverUtilizationList
        drivers={[
          driverFixture({ driver_id: "drv-001", driver_name: "John Smith" }),
          driverFixture({
            driver_id: "drv-002",
            driver_name: "Jane Doe",
            active_order_count: 7,
          }),
        ]}
      />,
    );

    expect(screen.getByText("drv-001")).toBeInTheDocument();
    expect(screen.getByText("John Smith")).toBeInTheDocument();
    expect(screen.getByText("drv-002")).toBeInTheDocument();
    expect(screen.getByText("Jane Doe")).toBeInTheDocument();
  });

  it("shows Active Orders column (not Shipments)", () => {
    render(<DriverUtilizationList drivers={[driverFixture()]} />);
    expect(screen.getByText("Active Orders")).toBeInTheDocument();
    expect(screen.queryByText("Shipments")).not.toBeInTheDocument();
  });

  it("shows Completed Today column", () => {
    render(<DriverUtilizationList drivers={[driverFixture()]} />);
    expect(screen.getByText("Completed Today")).toBeInTheDocument();
  });

  it("shows Medical Card column", () => {
    render(<DriverUtilizationList drivers={[driverFixture()]} />);
    expect(screen.getByText("Medical Card")).toBeInTheDocument();
  });

  it("renders empty state when no drivers", () => {
    render(<DriverUtilizationList drivers={[]} />);
    expect(screen.getByText(/no drivers found/i)).toBeInTheDocument();
  });
});

describe("DriverUtilizationList — medical card warning", () => {
  it("shows Expiring soon for medical card within 30 days", () => {
    const soon = new Date();
    soon.setDate(soon.getDate() + 15);

    render(
      <DriverUtilizationList
        drivers={[driverFixture({ medical_card_expiry: soon.toISOString() })]}
      />,
    );

    expect(screen.getByText("Expiring soon")).toBeInTheDocument();
  });

  it("shows Expired for past medical card", () => {
    render(
      <DriverUtilizationList
        drivers={[
          driverFixture({ medical_card_expiry: "2020-01-01T00:00:00Z" }),
        ]}
      />,
    );

    expect(screen.getByText("Expired")).toBeInTheDocument();
  });

  it("does not show warning for far-future medical card", () => {
    render(
      <DriverUtilizationList
        drivers={[
          driverFixture({ medical_card_expiry: "2030-01-01T00:00:00Z" }),
        ]}
      />,
    );

    expect(screen.queryByText("Expiring soon")).not.toBeInTheDocument();
    expect(screen.queryByText("Expired")).not.toBeInTheDocument();
  });
});

describe("DriverUtilizationList — status filter", () => {
  it("filters drivers by status when filter is applied", () => {
    const onFilter = jest.fn();
    render(
      <DriverUtilizationList
        drivers={[
          driverFixture({ driver_id: "drv-active", status: "active" }),
          driverFixture({ driver_id: "drv-off", status: "off_duty" }),
        ]}
        statusFilter="active"
        onStatusFilterChange={onFilter}
      />,
    );

    expect(screen.getByText("drv-active")).toBeInTheDocument();
    expect(screen.queryByText("drv-off")).not.toBeInTheDocument();
  });

  it("calls onStatusFilterChange when filter select changes", () => {
    const onFilter = jest.fn();
    render(
      <DriverUtilizationList
        drivers={[driverFixture()]}
        statusFilter=""
        onStatusFilterChange={onFilter}
      />,
    );

    fireEvent.change(screen.getByLabelText(/filter drivers by status/i), {
      target: { value: "on_break" },
    });

    expect(onFilter).toHaveBeenCalledWith("on_break");
  });
});

describe("DriverUtilizationList — sorting", () => {
  it("sorts by active_order_count when column header is clicked", () => {
    render(
      <DriverUtilizationList
        drivers={[
          driverFixture({ driver_id: "drv-low", active_order_count: 1 }),
          driverFixture({ driver_id: "drv-high", active_order_count: 10 }),
        ]}
      />,
    );

    // Click Active Orders header to sort
    fireEvent.click(screen.getByText("Active Orders"));

    const rows = screen.getAllByRole("row");
    // First data row (after header) should be the high one (desc by default)
    expect(rows[1]).toHaveTextContent("drv-high");
  });
});

describe("DriverUtilizationList — utilization bar", () => {
  it("renders utilization progressbar for each driver", () => {
    render(
      <DriverUtilizationList
        drivers={[driverFixture({ active_order_count: 4 })]}
        capacity={8}
      />,
    );

    const progressbar = screen.getByRole("progressbar");
    expect(progressbar).toHaveAttribute("aria-valuenow", "50");
  });
});

describe("DriverUtilizationList — Fleet truck link (Req 4.1, 13.1)", () => {
  it("renders assigned_truck_id as a link into the Fleet module", () => {
    render(
      <DriverUtilizationList
        drivers={[driverFixture({ assigned_truck_id: "TRK-100" })]}
      />,
    );
    const link = screen.getByRole("link", { name: "TRK-100" });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute("href", "/dashboard");
  });

  it("sets the Fleet module as active when the truck link is clicked", () => {
    render(
      <DriverUtilizationList
        drivers={[driverFixture({ assigned_truck_id: "TRK-100" })]}
      />,
    );
    fireEvent.click(screen.getByRole("link", { name: "TRK-100" }));
    expect(window.sessionStorage.getItem("activeMenuItem")).toBe("fleet");
  });

  it("renders a dash when no truck is assigned", () => {
    render(
      <DriverUtilizationList
        drivers={[driverFixture({ assigned_truck_id: null })]}
      />,
    );
    expect(
      screen.queryByRole("link", { name: /TRK-/ }),
    ).not.toBeInTheDocument();
  });
});

describe("DriverUtilizationList — qualification chip (Req 4.3, 13.3)", () => {
  // Use a far-future medical card so the medical-card column does not also
  // render an "Expired"/"Expiring" label that collides with the chip assertion.
  const farFuture = "2099-01-01T00:00:00Z";

  it("shows an Expired chip when qualification_status is expired", () => {
    render(
      <DriverUtilizationList
        drivers={[
          driverFixture({
            qualification_status: "expired",
            medical_card_expiry: farFuture,
          }),
        ]}
      />,
    );
    expect(screen.getByText("Expired")).toBeInTheDocument();
  });

  it("shows an Expiring chip when qualification_status is expiring", () => {
    render(
      <DriverUtilizationList
        drivers={[
          driverFixture({
            qualification_status: "expiring",
            medical_card_expiry: farFuture,
          }),
        ]}
      />,
    );
    expect(screen.getByText("Expiring")).toBeInTheDocument();
  });

  it("shows a Valid chip when qualification_status is valid", () => {
    render(
      <DriverUtilizationList
        drivers={[
          driverFixture({
            qualification_status: "valid",
            medical_card_expiry: farFuture,
          }),
        ]}
      />,
    );
    expect(screen.getByText("Valid")).toBeInTheDocument();
  });

  it("shows an Unlinked affordance when qualification is unresolved", () => {
    render(
      <DriverUtilizationList
        drivers={[
          driverFixture({
            qualification_status: null,
            medical_card_expiry: farFuture,
          }),
        ]}
      />,
    );
    expect(screen.getByText("Unlinked")).toBeInTheDocument();
  });
});
