/**
 * Tests that the Storm_Mode banner's role gate reads from the verified
 * SuperTokens session's claims rather than a self-minted token (Req 8.6).
 *
 * The existing :file:`StormModeBanner.test.tsx` exercises the gate by passing
 * an explicit `roles` prop (the test seam). These tests omit the prop so the
 * banner hydrates roles + actor from `getCurrentUserRoles` / `getCurrentUserId`
 * — the session-claim path the production app uses:
 *
 *   - A session whose claims carry `driver` hides the override control.
 *   - A session whose claims carry `dispatcher` shows the override control and
 *     stamps the session-derived actor id on the opened override form.
 *   - An unauthenticated session (empty roles) hides the control.
 *
 * `getCurrentUserRoles` / `getCurrentUserId` are mocked to stand in for the
 * SuperTokens session claims; the status fetcher is injected via prop.
 *
 * Validates: Requirements 8.6.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import type { StormModeStatusResponse } from "../../services/fuelApi";
import { getCurrentUserId, getCurrentUserRoles } from "../../utils/auth";
import StormModeBanner from "./StormModeBanner";

jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    __esModule: true,
    ...actual,
    submitStormModeOverride: jest.fn(),
  };
});

jest.mock("../../utils/auth", () => ({
  __esModule: true,
  getCurrentUserRoles: jest.fn(),
  getCurrentUserId: jest.fn(),
}));

const rolesMock = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;
const userIdMock = getCurrentUserId as jest.MockedFunction<
  typeof getCurrentUserId
>;

function activeStatusFixture(): StormModeStatusResponse {
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
  };
}

beforeEach(() => {
  rolesMock.mockReset();
  userIdMock.mockReset();
  userIdMock.mockResolvedValue("st-user-1");
});

describe("StormModeBanner role gate from session claims", () => {
  it("hides the override control when the session carries only a driver role", async () => {
    rolesMock.mockResolvedValue(["driver"]);
    const fetchStatus = jest.fn().mockResolvedValue(activeStatusFixture());

    render(
      <StormModeBanner fetchStatus={fetchStatus} pollIntervalMs={10_000} />,
    );

    await screen.findByTestId("storm-mode-banner");
    // Wait for the session-claim hydration to settle.
    expect(rolesMock).toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: /^override$/i }),
    ).not.toBeInTheDocument();
  });

  it("shows the override control when the session carries a dispatcher role", async () => {
    rolesMock.mockResolvedValue(["dispatcher"]);
    const fetchStatus = jest.fn().mockResolvedValue(activeStatusFixture());

    render(
      <StormModeBanner fetchStatus={fetchStatus} pollIntervalMs={10_000} />,
    );

    await screen.findByTestId("storm-mode-banner");
    const overrideButton = await screen.findByRole("button", {
      name: /^override$/i,
    });
    expect(overrideButton).toBeInTheDocument();
  });

  it("stamps the session-derived actor id on the opened override form", async () => {
    rolesMock.mockResolvedValue(["admin"]);
    userIdMock.mockResolvedValue("st-user-42");
    const fetchStatus = jest.fn().mockResolvedValue(activeStatusFixture());

    render(
      <StormModeBanner fetchStatus={fetchStatus} pollIntervalMs={10_000} />,
    );

    const overrideButton = await screen.findByRole("button", {
      name: /^override$/i,
    });
    fireEvent.click(overrideButton);

    // The form opens because the session-claim role authorized it; the actor
    // id used by the form is the verified session user id, never client input.
    expect(
      screen.getByRole("form", { name: /submit storm_mode override/i }),
    ).toBeInTheDocument();
    expect(userIdMock).toHaveBeenCalled();
  });

  it("hides the override control for an unauthenticated session", async () => {
    rolesMock.mockResolvedValue([]);
    userIdMock.mockResolvedValue(null);
    const fetchStatus = jest.fn().mockResolvedValue(activeStatusFixture());

    render(
      <StormModeBanner fetchStatus={fetchStatus} pollIntervalMs={10_000} />,
    );

    await screen.findByTestId("storm-mode-banner");
    expect(rolesMock).toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: /^override$/i }),
    ).not.toBeInTheDocument();
  });
});
