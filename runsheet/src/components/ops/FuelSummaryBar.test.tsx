/**
 * F5: the network "Avg Days Until Empty" excludes stations with no
 * consumption (the 99999 sentinel), and a station with no consumption shows
 * "—" instead of "99999.0 days".
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { FuelNetworkSummary, FuelStation } from "../../services/fuelApi";
import { displayDaysUntilEmpty } from "../../services/fuelApi";
import FuelStationList from "./FuelStationList";
import FuelSummaryBar from "./FuelSummaryBar";

const SUMMARY: FuelNetworkSummary = {
  total_stations: 3,
  total_capacity_liters: 64000,
  total_current_stock_liters: 40050,
  total_daily_consumption: 135.71,
  average_days_until_empty: 217.7,
  stations_normal: 3,
  stations_low: 0,
  stations_critical: 0,
  stations_empty: 0,
  active_alerts: 0,
};

function station(
  id: string,
  days: number,
  rate: number,
  name = id,
): FuelStation {
  return {
    station_id: id,
    name,
    fuel_type: "AGO",
    capacity_liters: 20000,
    current_stock_liters: 10000,
    daily_consumption_rate: rate,
    days_until_empty: days,
    alert_threshold_pct: 20,
    status: "normal",
    tenant_id: "demo-tenant",
    last_updated: "2026-10-08T00:00:00Z",
  };
}

describe("FuelSummaryBar", () => {
  it("shows the backend average", () => {
    render(<FuelSummaryBar summary={SUMMARY} />);
    expect(screen.getByText("217.7 days")).toBeInTheDocument();
  });

  it("shows '—' when no station consumes fuel (null)", () => {
    render(
      <FuelSummaryBar
        summary={{ ...SUMMARY, average_days_until_empty: null }}
      />,
    );
    const region = screen.getByRole("region", {
      name: "Fuel network summary",
    });
    expect(within(region).getByText("—")).toBeInTheDocument();
    expect(region).not.toHaveTextContent("99999");
    expect(region).not.toHaveTextContent("N/A");
  });
});

describe("FuelStationList days left", () => {
  const stations = [
    station("S-IDLE", 99999, 0, "Idle depot"),
    station("S-A", 321.1, 50, "Alpha"),
    station("S-B", 114.3, 85.71, "Bravo"),
  ];

  it("renders '—' for a station with no consumption, never 99999", () => {
    render(<FuelStationList stations={stations} />);
    expect(screen.queryByText(/99999/)).not.toBeInTheDocument();
    expect(screen.getByText("321.1 days")).toBeInTheDocument();
    const idleRow = screen.getByText("Idle depot").closest("tr");
    expect(idleRow).not.toBeNull();
    expect(idleRow).not.toHaveTextContent("days");
    expect(
      within(idleRow as HTMLElement).getAllByText("—").length,
    ).toBeGreaterThan(0);
  });

  it("sorts no-consumption stations last in both directions", () => {
    render(<FuelStationList stations={stations} />);
    const order = () =>
      screen
        .getAllByRole("row")
        .map((r) => r.getAttribute("data-row-id"))
        .filter(Boolean);

    fireEvent.click(screen.getByText("Days Left"));
    expect(order()).toEqual(["S-A", "S-B", "S-IDLE"]); // descending
    fireEvent.click(screen.getByText("Days Left"));
    expect(order()).toEqual(["S-B", "S-A", "S-IDLE"]); // ascending
  });
});

describe("displayDaysUntilEmpty", () => {
  it("hides the sentinel and zero-consumption stations", () => {
    expect(displayDaysUntilEmpty({ days_until_empty: 99999 })).toBeNull();
    expect(
      displayDaysUntilEmpty({
        days_until_empty: 12,
        daily_consumption_rate: 0,
      }),
    ).toBeNull();
    expect(displayDaysUntilEmpty({ days_until_empty: 0 })).toBeNull();
    expect(
      displayDaysUntilEmpty({
        days_until_empty: 4.5,
        daily_consumption_rate: 2,
      }),
    ).toBe(4.5);
  });
});
