/**
 * Regression tests for IFTAReportPage.
 *
 * The backend returns ``fleet_mpg: Optional[float]`` — it is ``null`` for a
 * quarter with no fuel gallons recorded. The page's ``formatNumber`` helper
 * previously assumed a non-null number and crashed with
 * "Cannot read properties of null (reading 'toLocaleString')". These tests
 * lock in the null-safe rendering.
 */
import "@testing-library/jest-dom";
import { render, screen, waitFor } from "@testing-library/react";

jest.mock("../../services/complianceApi", () => {
  const actual = jest.requireActual("../../services/complianceApi");
  return {
    ...actual,
    getIFTAReport: jest.fn(),
    createMileageAdjustment: jest.fn(),
  };
});

import type { IFTAReport } from "../../services/complianceApi";
import { getIFTAReport } from "../../services/complianceApi";
import IFTAReportPage from "./IFTAReportPage";

const mockGetReport = getIFTAReport as jest.MockedFunction<
  typeof getIFTAReport
>;

function reportFixture(overrides: Partial<IFTAReport> = {}): IFTAReport {
  return {
    tenant_id: "tenant-a",
    quarter: "2026-Q2",
    trucks: [],
    fleet_mpg: null,
    incomplete_trucks: [],
    generated_at: "2026-06-05T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  mockGetReport.mockReset();
});

it("renders without crashing when fleet_mpg is null", async () => {
  mockGetReport.mockResolvedValue({
    data: reportFixture({ fleet_mpg: null }),
    request_id: "r",
  });

  render(<IFTAReportPage />);

  // The Fleet MPG card renders an em-dash placeholder rather than throwing.
  await waitFor(() => {
    expect(screen.getByText("Fleet MPG")).toBeInTheDocument();
  });
  expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(1);
});

it("renders incomplete-truck flags (objects, not strings) with unique keys", async () => {
  mockGetReport.mockResolvedValue({
    data: reportFixture({
      incomplete_trucks: [
        {
          truck_id: "TRK-009",
          flag_type: "ifta_data_incomplete",
          quarter: "2026-Q2",
          reason: "No Geotab mileage recorded",
          flagged_at: "2026-06-05T00:00:00Z",
        },
        {
          truck_id: "TRK-010",
          flag_type: "ifta_data_incomplete",
          quarter: "2026-Q2",
          reason: "No Geotab mileage recorded",
          flagged_at: "2026-06-05T00:00:00Z",
        },
      ],
    }),
    request_id: "r",
  });

  render(<IFTAReportPage />);

  // Each flag renders its truck_id (not "[object Object]").
  await waitFor(() => {
    expect(screen.getByText("TRK-009")).toBeInTheDocument();
  });
  expect(screen.getByText("TRK-010")).toBeInTheDocument();
  expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
});

it("renders a truck row with null per-truck fleet_mpg without crashing", async () => {
  mockGetReport.mockResolvedValue({
    data: reportFixture({
      fleet_mpg: 6.2,
      trucks: [
        {
          truck_id: "TRK-001",
          truck_name: "Rig 1",
          jurisdictions: [],
          total_miles: 1200,
          total_gallons: 0,
          fleet_mpg: null,
        },
      ],
    }),
    request_id: "r",
  });

  render(<IFTAReportPage />);

  await waitFor(() => {
    expect(screen.getByText("TRK-001")).toBeInTheDocument();
  });
  // Report-level MPG renders its real value; the null per-truck MPG shows "—".
  expect(screen.getByText("6.20")).toBeInTheDocument();
  expect(screen.getByText("1,200.0")).toBeInTheDocument();
});
