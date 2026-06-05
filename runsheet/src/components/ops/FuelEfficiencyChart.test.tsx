/**
 * Tests for the Fleet Fuel Efficiency chart.
 *
 * Guards the data-contract fix: the backend returns
 * ``{ asset_id, total_liters, total_distance_km, liters_per_km, event_count }``
 * (efficiency as liters-per-km, nullable when no odometer data), and the
 * chart must render km/L (the reciprocal) — NOT read the old
 * ``distance_km`` / ``fuel_consumed_liters`` / ``efficiency_km_per_liter``
 * field names that silently resolved to 0.
 */
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";

jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    ...actual,
    getEfficiencyMetrics: jest.fn(),
  };
});

import type { EfficiencyMetric } from "../../services/fuelApi";
import { getEfficiencyMetrics } from "../../services/fuelApi";
import FuelEfficiencyChart from "./FuelEfficiencyChart";

const mockGet = getEfficiencyMetrics as jest.MockedFunction<
  typeof getEfficiencyMetrics
>;

function metric(overrides: Partial<EfficiencyMetric> = {}): EfficiencyMetric {
  return {
    asset_id: "TRK-100",
    total_liters: 200,
    total_distance_km: 800,
    liters_per_km: 0.25, // → 4.00 km/L
    event_count: 3,
    ...overrides,
  };
}

beforeEach(() => {
  mockGet.mockReset();
});

it("renders km/L derived from the backend liters_per_km field", async () => {
  mockGet.mockResolvedValue({
    data: [metric({ asset_id: "TRK-100", liters_per_km: 0.25 })],
    request_id: "r",
  });

  render(<FuelEfficiencyChart />);

  // 1 / 0.25 = 4.00 km/L
  expect(await screen.findByText("4.00")).toBeInTheDocument();
  expect(screen.getByText("TRK-100")).toBeInTheDocument();
});

it("renders distance and fuel from total_distance_km / total_liters", async () => {
  mockGet.mockResolvedValue({
    data: [
      metric({
        total_distance_km: 800,
        total_liters: 200,
        liters_per_km: 0.25,
      }),
    ],
    request_id: "r",
  });

  render(<FuelEfficiencyChart />);

  // 800 km and 200 L render (not "0" from the old mismatched field names).
  expect(await screen.findByText("800.0")).toBeInTheDocument();
  expect(screen.getByText("200.0")).toBeInTheDocument();
});

it("shows a 'No odometer data' state when liters_per_km is null", async () => {
  mockGet.mockResolvedValue({
    data: [
      metric({
        asset_id: "TRK-200",
        total_distance_km: null,
        liters_per_km: null,
      }),
    ],
    request_id: "r",
  });

  render(<FuelEfficiencyChart />);

  expect(await screen.findByText(/No odometer data/i)).toBeInTheDocument();
});

it("renders the empty state when no assets come back", async () => {
  mockGet.mockResolvedValue({ data: [], request_id: "r" });

  render(<FuelEfficiencyChart />);

  expect(
    await screen.findByText(/No efficiency data available/i),
  ).toBeInTheDocument();
});
