/**
 * F5: the network "Avg … days left" excludes stations with no consumption
 * (the 99999 sentinel), and a station with no consumption shows "—" instead
 * of "99999.0 d". Ported from the go-live-blockers FuelSummaryBar test onto
 * the Phase 3 components (the summary bar became the Fuel title-row counts).
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { FuelStation } from "../../services/fuelApi";
import { displayDaysUntilEmpty } from "../../services/fuelApi";
import { avgDaysLeftLabel } from "./FuelDashboardView";
import FuelStationDetail from "./FuelStationDetail";
import FuelStationList from "./FuelStationList";

jest.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
  usePathname: () => "/dashboard/fuel",
}));
jest.mock("../../hooks/useOpsWebSocket", () => ({
  useOpsWebSocket: () => ({ connected: false }),
}));

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

describe("Fuel title-row average", () => {
  it("shows the backend average", () => {
    expect(avgDaysLeftLabel(217.7)).toBe("217.7");
  });

  it("shows '—' when no station consumes fuel (null)", () => {
    expect(avgDaysLeftLabel(null)).toBe("—");
    expect(avgDaysLeftLabel(0)).toBe("—");
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
    expect(screen.queryByText(/99,?999/)).not.toBeInTheDocument();
    expect(screen.getByText("321.1 d")).toBeInTheDocument();
    const idleRow = screen.getByText("Idle depot").closest("tr");
    expect(idleRow).not.toBeNull();
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
    const header = () => screen.getByRole("button", { name: /Days left/ });

    fireEvent.click(header());
    expect(order()).toEqual(["S-B", "S-A", "S-IDLE"]); // ascending
    fireEvent.click(header());
    expect(order()).toEqual(["S-A", "S-B", "S-IDLE"]); // descending
  });
});

describe("FuelStationDetail days left", () => {
  it("shows '—' for a station with no consumption", () => {
    render(
      <FuelStationDetail
        detail={{
          station: station("S-IDLE", 99999, 0, "Idle depot"),
          recent_consumption_events: [],
          recent_refill_events: [],
        }}
      />,
    );
    expect(screen.queryByText(/99,?999/)).not.toBeInTheDocument();
    expect(screen.getByText("Days left").parentElement).toHaveTextContent("—");
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
