/**
 * Tests for :file:`StormModeBanner.tsx`.
 *
 * Covers the rendering + interaction pathways the operations control
 * pages depend on:
 *
 *   - Hides when Storm_Mode is ``inactive``.
 *   - Hides gracefully when the evaluator is not configured (HTTP 503).
 *   - Renders triggering alert, activation window, override indicator
 *     and "Full details" link when state is ``active``.
 *   - Role-gated override form is hidden for non-dispatcher/admin
 *     callers and shown for dispatcher/admin roles.
 *   - Override submission forwards the full payload including the
 *     ``actor_id`` and trimmed ``reason``; surfaces HTTP 403 as a
 *     user-visible error.
 *   - Submit is blocked when reason is blank.
 *
 * Validates: Requirements 9.1.6, 9.4.1, 9.4.2, 9.4.3, 9.4.4.
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ApiError } from "../../services/api";
import type { StormModeStatusResponse } from "../../services/fuelApi";
import StormModeBanner, { canSubmitStormModeOverride } from "./StormModeBanner";

// Mock the submitStormModeOverride path; the status fetcher is injected
// via prop so the test can return deterministic responses without
// touching the network.
jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    __esModule: true,
    ...actual,
    submitStormModeOverride: jest.fn(),
  };
});

import { submitStormModeOverride } from "../../services/fuelApi";

const submitMock = submitStormModeOverride as jest.MockedFunction<
  typeof submitStormModeOverride
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

function inactiveStatusFixture(): StormModeStatusResponse {
  return {
    tenant_id: "tenant-a",
    state: "inactive",
    computed_state: "inactive",
    override_active: false,
    override: null,
    triggering_alerts: [],
    activation_window: {
      lookahead_hours: 48,
      severity_threshold: "severe",
      activated_at: null,
      clears_at: null,
    },
    updated_at: null,
  };
}

describe("canSubmitStormModeOverride", () => {
  it("allows dispatcher and admin roles case-insensitively", () => {
    expect(canSubmitStormModeOverride(["dispatcher"])).toBe(true);
    expect(canSubmitStormModeOverride(["Admin"])).toBe(true);
    expect(canSubmitStormModeOverride(["dispatcher_lead"])).toBe(true);
    expect(canSubmitStormModeOverride(["ops_admin"])).toBe(true);
  });

  it("rejects other roles and empty inputs", () => {
    expect(canSubmitStormModeOverride(["driver"])).toBe(false);
    expect(canSubmitStormModeOverride([])).toBe(false);
    expect(canSubmitStormModeOverride(null)).toBe(false);
    expect(canSubmitStormModeOverride(undefined)).toBe(false);
  });
});

describe("StormModeBanner", () => {
  beforeEach(() => {
    submitMock.mockReset();
  });

  it("renders nothing while the initial fetch is pending", () => {
    const fetchStatus = jest.fn(
      () => new Promise<StormModeStatusResponse>(() => {}),
    );
    const { container } = render(
      <StormModeBanner fetchStatus={fetchStatus} pollIntervalMs={10_000} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("stays hidden when Storm_Mode is inactive", async () => {
    const fetchStatus = jest.fn().mockResolvedValue(inactiveStatusFixture());
    const { container } = render(
      <StormModeBanner fetchStatus={fetchStatus} pollIntervalMs={10_000} />,
    );
    await waitFor(() => expect(fetchStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("stays hidden when the evaluator is not configured (HTTP 503)", async () => {
    const fetchStatus = jest
      .fn()
      .mockRejectedValue(new ApiError("storm_mode_evaluator_unavailable", 503));
    const { container } = render(
      <StormModeBanner fetchStatus={fetchStatus} pollIntervalMs={10_000} />,
    );
    await waitFor(() => expect(fetchStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders triggering alert + activation window when state is active", async () => {
    const fetchStatus = jest.fn().mockResolvedValue(activeStatusFixture());
    render(
      <StormModeBanner fetchStatus={fetchStatus} pollIntervalMs={10_000} />,
    );
    const banner = await screen.findByTestId("storm-mode-banner");
    expect(banner).toHaveTextContent("Storm_Mode active");
    expect(banner).toHaveTextContent("Winter Storm");
    expect(banner).toHaveTextContent("Franklin County");
    expect(banner).toHaveTextContent("severe");
    // Activation window labels
    expect(banner).toHaveTextContent(/Activated:/);
    expect(banner).toHaveTextContent(/Expected clear:/);
    expect(banner).toHaveTextContent(/Threshold:/);
    // Source + ZIP footprint
    expect(banner).toHaveTextContent("NWS");
    expect(banner).toHaveTextContent(/2 ZIPs/);
    // Full details link present
    expect(screen.getByRole("link", { name: /full details/i })).toHaveAttribute(
      "href",
      "/admin/weather-alerts",
    );
  });

  it("surfaces the override indicator when an override is active", async () => {
    const fetchStatus = jest.fn().mockResolvedValue(
      activeStatusFixture({
        override_active: true,
        override: {
          override_id: "smo_1",
          action: "activate",
          reason: "ops force-on",
          actor_id: "dispatcher-007",
          expires_at: "2024-01-18T00:00:00Z",
        },
      }),
    );
    render(
      <StormModeBanner fetchStatus={fetchStatus} pollIntervalMs={10_000} />,
    );
    const banner = await screen.findByTestId("storm-mode-banner");
    expect(banner).toHaveTextContent(/Override:/);
    expect(banner).toHaveTextContent(/Activate/);
    expect(banner).toHaveTextContent("dispatcher-007");
  });

  it("hides the override button for non-dispatcher/admin callers", async () => {
    const fetchStatus = jest.fn().mockResolvedValue(activeStatusFixture());
    render(
      <StormModeBanner
        fetchStatus={fetchStatus}
        pollIntervalMs={10_000}
        roles={["driver"]}
      />,
    );
    await screen.findByTestId("storm-mode-banner");
    expect(
      screen.queryByRole("button", { name: /^override$/i }),
    ).not.toBeInTheDocument();
  });

  it("shows the override button for dispatcher role and opens the form on click", async () => {
    const fetchStatus = jest.fn().mockResolvedValue(activeStatusFixture());
    render(
      <StormModeBanner
        fetchStatus={fetchStatus}
        pollIntervalMs={10_000}
        roles={["dispatcher"]}
      />,
    );
    const overrideButton = await screen.findByRole("button", {
      name: /^override$/i,
    });
    fireEvent.click(overrideButton);
    expect(
      screen.getByRole("form", { name: /submit storm_mode override/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /submit override/i }),
    ).toBeInTheDocument();
  });

  it("requires a non-blank reason before submitting", async () => {
    const fetchStatus = jest.fn().mockResolvedValue(activeStatusFixture());
    render(
      <StormModeBanner
        fetchStatus={fetchStatus}
        pollIntervalMs={10_000}
        roles={["admin"]}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /^override$/i }));
    const form = screen.getByRole("form", {
      name: /submit storm_mode override/i,
    });
    const submitBtn = screen.getByRole("button", { name: /submit override/i });
    // Leave reason blank: override jsdom's form-check by firing submit on
    // the form element directly so the handler runs and surfaces the
    // "reason required" error.
    fireEvent.submit(form);
    await screen.findByText(/reason is required/i);
    expect(submitMock).not.toHaveBeenCalled();
    // Still visible after validation failure.
    expect(submitBtn).toBeInTheDocument();
  });

  it("forwards the payload and refreshes status on successful submit", async () => {
    const fetchStatus = jest
      .fn()
      .mockResolvedValueOnce(activeStatusFixture())
      .mockResolvedValueOnce(inactiveStatusFixture());
    submitMock.mockResolvedValue({
      override_id: "smo_1",
      tenant_id: "tenant-a",
      action: "deactivate",
      reason: "Region clear",
      actor_id: "dispatcher-007",
      expires_at: null,
    });

    render(
      <StormModeBanner
        fetchStatus={fetchStatus}
        pollIntervalMs={10_000}
        roles={["dispatcher"]}
        actorId="dispatcher-007"
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /^override$/i }));
    fireEvent.change(screen.getByPlaceholderText(/explain why/i), {
      target: { value: "  Region clear  " },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /submit override/i }));
    });

    await waitFor(() =>
      expect(submitMock).toHaveBeenCalledWith({
        action: "deactivate",
        reason: "Region clear",
        actor_id: "dispatcher-007",
        expires_at: null,
      }),
    );
    // After submit, banner re-polls; second fetch reports inactive so
    // the banner hides itself.
    await waitFor(() =>
      expect(screen.queryByTestId("storm-mode-banner")).not.toBeInTheDocument(),
    );
  });

  it("surfaces HTTP 403 forbidden_role without hiding the form", async () => {
    const fetchStatus = jest.fn().mockResolvedValue(activeStatusFixture());
    submitMock.mockRejectedValue(new ApiError("forbidden_role", 403));

    render(
      <StormModeBanner
        fetchStatus={fetchStatus}
        pollIntervalMs={10_000}
        roles={["dispatcher_lead"]}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /^override$/i }));
    fireEvent.change(screen.getByPlaceholderText(/explain why/i), {
      target: { value: "Test" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /submit override/i }));
    });
    await screen.findByText(/don't have permission/i);
    // Form stays mounted so operator can retry or escalate.
    expect(
      screen.getByRole("form", { name: /submit storm_mode override/i }),
    ).toBeInTheDocument();
  });
});
