/**
 * Tests for the Weather Alerts / Storm_Mode detail page
 * (:file:`/admin/weather-alerts/page.tsx`).
 *
 * This page is the "Full details" destination linked from
 * :file:`components/ops/StormModeBanner.tsx`. The route previously 404'd
 * because no ``page.tsx`` existed at ``/admin/weather-alerts``; these
 * tests pin the contract so it doesn't silently regress:
 *
 *   - Renders the active Storm_Mode posture + triggering alerts table.
 *   - Treats HTTP 503 (evaluator not wired) as an informational empty
 *     state rather than a red error screen.
 *   - Gates the override control to dispatcher / admin roles.
 *
 * Both ``getStormModeStatus`` and ``listStormRoadRestrictions`` are
 * mocked so the test never touches the network; the embedded
 * RoadRestrictionsPanel loads its own data via the same mocked module.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { ApiError } from "../../../services/api";
import type { StormModeStatusResponse } from "../../../services/fuelApi";

jest.mock("../../../services/fuelApi", () => {
  const actual = jest.requireActual("../../../services/fuelApi");
  return {
    __esModule: true,
    ...actual,
    getStormModeStatus: jest.fn(),
    listStormRoadRestrictions: jest.fn(),
    submitStormModeOverride: jest.fn(),
  };
});

jest.mock("../../../utils/auth", () => ({
  __esModule: true,
  getAuthToken: jest.fn().mockResolvedValue("token"),
  getCurrentUserRoles: jest.fn(),
}));

import {
  getStormModeStatus,
  listStormRoadRestrictions,
} from "../../../services/fuelApi";
import { getCurrentUserRoles } from "../../../utils/auth";
import WeatherAlertsPage from "./page";

const statusMock = getStormModeStatus as jest.MockedFunction<
  typeof getStormModeStatus
>;
const restrictionsMock = listStormRoadRestrictions as jest.MockedFunction<
  typeof listStormRoadRestrictions
>;
const rolesMock = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;

function activeStatusFixture(
  overrides: Partial<StormModeStatusResponse> = {},
): StormModeStatusResponse {
  return {
    tenant_id: "tenant-a",
    state: "active",
    computed_state: "active",
    override_active: false,
    override: null,
    triggering_alerts: [
      {
        alert_id: "alert-1",
        alert_type: "winter_storm",
        severity: "severe",
        headline: "Winter Storm Warning for Franklin County",
        description: null,
        expected_start_at: "2024-01-15T12:00:00Z",
        expected_end_at: "2024-01-17T12:00:00Z",
        affected_zip_codes: ["43215", "43220"],
        source: "nws",
        activation_status: "active",
      },
    ],
    activation_window: {
      lookahead_hours: 48,
      severity_threshold: "severe",
      activated_at: "2024-01-15T10:00:00Z",
      clears_at: "2024-01-17T12:00:00Z",
    },
    updated_at: "2024-01-15T10:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  statusMock.mockReset();
  restrictionsMock.mockReset();
  rolesMock.mockReset();
  restrictionsMock.mockResolvedValue({ items: [], total: 0 });
  rolesMock.mockResolvedValue([]);
});

describe("WeatherAlertsPage", () => {
  it("renders the active posture and the triggering alert row", async () => {
    statusMock.mockResolvedValue(activeStatusFixture());

    render(<WeatherAlertsPage />);

    expect(
      await screen.findByTestId("storm-mode-status-card"),
    ).toBeInTheDocument();
    expect(screen.getByText("Storm_Mode active")).toBeInTheDocument();
    // The triggering alert surfaces in the shared Table.
    expect(screen.getByText("Winter Storm")).toBeInTheDocument();
    expect(
      screen.getByText("Winter Storm Warning for Franklin County"),
    ).toBeInTheDocument();
  });

  it("treats a 503 (evaluator not wired) as an informational empty state", async () => {
    statusMock.mockRejectedValue(
      new ApiError("storm_mode_evaluator_unavailable", 503),
    );

    render(<WeatherAlertsPage />);

    expect(
      await screen.findByText(
        /Storm_Mode evaluator is not configured for this environment/i,
      ),
    ).toBeInTheDocument();
    // No status card, no error alert.
    expect(
      screen.queryByTestId("storm-mode-status-card"),
    ).not.toBeInTheDocument();
  });

  it("hides the override control for non-dispatcher / non-admin roles", async () => {
    statusMock.mockResolvedValue(activeStatusFixture());
    rolesMock.mockResolvedValue(["viewer"]);

    render(<WeatherAlertsPage />);

    await screen.findByTestId("storm-mode-status-card");
    expect(
      screen.queryByRole("button", { name: /^Override$/ }),
    ).not.toBeInTheDocument();
  });

  it("shows the override control for dispatcher / admin roles", async () => {
    statusMock.mockResolvedValue(activeStatusFixture());
    rolesMock.mockResolvedValue(["admin"]);

    render(<WeatherAlertsPage />);

    await screen.findByTestId("storm-mode-status-card");
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /^Override$/ }),
      ).toBeInTheDocument();
    });
  });
});
