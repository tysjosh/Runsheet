/**
 * Live (UI revamp task 2.6, R9.1–R9.3): tabs in the title row, counts, the
 * autonomy chip linking to Settings → Agents, the truck list with identity
 * avatars and status badges instead of the lat/lon grid, exceptions and
 * delays, active jobs, and the approvals inbox focused by `?id=`.
 */
import "@testing-library/jest-dom";
import { render, screen, waitFor, within } from "@testing-library/react";

let mockParams = new URLSearchParams();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
  usePathname: () => "/dashboard/control",
  useSearchParams: () => mockParams,
}));
jest.mock("../../services/schedulingApi", () => ({
  getActiveJobs: jest.fn(),
  getDelayedJobs: jest.fn(),
}));
jest.mock("../../services/fuelApi", () => ({ getAlerts: jest.fn() }));
jest.mock("../../services/inventoryApi", () => ({ getAlerts: jest.fn() }));
jest.mock("../../services/api", () => ({
  ...jest.requireActual("../../services/api"),
  apiService: { getAssets: jest.fn() },
}));
jest.mock("../../services/agentApi", () => ({
  ...jest.requireActual("../../services/agentApi"),
  getAutonomyLevel: jest.fn(),
  getApprovals: jest.fn(),
}));
jest.mock("../../utils/auth", () => ({
  ...jest.requireActual("../../utils/auth"),
  getCurrentUserRoles: jest.fn(async () => ["admin"]),
}));
jest.mock("../../hooks/useSchedulingWebSocket", () => ({
  useSchedulingWebSocket: jest.fn(),
}));
jest.mock("../../hooks/useAgentWebSocket", () => ({
  useAgentWebSocket: jest.fn(),
}));
jest.mock("../ops/StormModeBanner", () => ({
  __esModule: true,
  default: () => null,
}));
jest.mock("../ops/DefaultDepotWarning", () => ({
  __esModule: true,
  default: () => null,
}));
jest.mock("../ops/AgentActivityFeed", () => ({
  __esModule: true,
  default: () => <div>Agent activity feed</div>,
}));
jest.mock("../ops/AgentHealth", () => ({
  __esModule: true,
  default: () => <div>Agent health panel</div>,
}));

import { getApprovals, getAutonomyLevel } from "../../services/agentApi";
import { apiService } from "../../services/api";
import { getAlerts as getFuelAlerts } from "../../services/fuelApi";
import { getAlerts as getInventoryAlerts } from "../../services/inventoryApi";
import { getActiveJobs, getDelayedJobs } from "../../services/schedulingApi";
import { getCurrentUserRoles } from "../../utils/auth";
import LiveView from "./LiveView";

const m = <T,>(f: T) => f as unknown as jest.Mock;
const job = (id: string, extra: Record<string, unknown> = {}) => ({
  job_id: id,
  job_type: "fuel_delivery",
  status: "in_progress",
  origin: "Terminal",
  destination: `Site ${id}`,
  asset_assigned: "TRK-1",
  delayed: false,
  estimated_arrival: "2026-10-08T15:30:00Z",
  ...extra,
});

beforeEach(() => {
  jest.clearAllMocks();
  mockParams = new URLSearchParams();
  m(getCurrentUserRoles).mockResolvedValue(["admin"]);
  m(getActiveJobs).mockResolvedValue({
    data: [
      job("J1"),
      job("J2", {
        status: "failed",
        failure_reason: "Site closed",
        asset_assigned: "TRK-2",
      }),
    ],
  });
  m(getDelayedJobs).mockResolvedValue({
    data: [
      job("J3", {
        delayed: true,
        delay_duration_minutes: 45,
        asset_assigned: "TRK-3",
      }),
    ],
  });
  m(getFuelAlerts).mockResolvedValue({
    data: [
      {
        station_id: "S1",
        name: "Yard tank",
        fuel_type: "KEROSENE",
        status: "critical",
        stock_percentage: 12,
      },
    ],
  });
  m(getInventoryAlerts).mockResolvedValue({ data: [{}, {}], count: 2 });
  m(apiService.getAssets).mockResolvedValue({
    data: [
      {
        id: "TRK-1",
        name: "TRK-1",
        currentLocation: { coordinates: { lat: 29.7, lon: -95.3 } },
      },
    ],
  });
  m(getAutonomyLevel).mockResolvedValue({ level: "auto-low" });
  m(getApprovals).mockResolvedValue({
    entries: [
      {
        action_id: "A1",
        tool_name: "route_plan",
        proposed_by: "route_agent",
        risk_level: "low",
        status: "pending",
        impact_summary: "Route plan",
        expiry_time: "2099-01-01T00:00:00Z",
      },
      {
        action_id: "A2",
        tool_name: "refill",
        proposed_by: "fuel_agent",
        risk_level: "low",
        status: "pending",
        impact_summary: "Refill",
        expiry_time: "2099-01-01T00:00:00Z",
      },
    ],
  });
});

describe("LiveView", () => {
  it("one h1 with Overview · Approvals · Agents tabs, counts and the autonomy chip", async () => {
    render(<LiveView />);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Approvals" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Agents" })).toBeInTheDocument();
    const counts = await screen.findByRole("list", { name: "Live counts" });
    expect(counts).toHaveTextContent("1 delayed");
    expect(counts).toHaveTextContent("1 exception");
    expect(
      within(counts).getByRole("link", { name: /2 inventory alerts/ }),
    ).toHaveAttribute("href", "/dashboard/fleet?tab=inventory");
    expect(
      await screen.findByRole("link", {
        name: /Agent autonomy: Auto \(low risk\)/,
      }),
    ).toHaveAttribute("href", "/dashboard/settings?tab=agents");
  });

  it("shows the autonomy chip as text, not a link, to a dispatcher", async () => {
    m(getCurrentUserRoles).mockResolvedValue(["dispatcher"]);
    render(<LiveView />);
    expect(
      await screen.findByText(/Agent autonomy:/, { selector: "span" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Agent autonomy/ }),
    ).not.toBeInTheDocument();
  });
  it("lists trucks with status, not coordinates (R9.3)", async () => {
    render(<LiveView />);
    const trucks = await screen.findByRole("list", {
      name: "Trucks on the map",
    });
    expect(trucks).toHaveTextContent("TRK-1");
    expect(trucks).toHaveTextContent("In transit");
    expect(document.body.textContent).not.toMatch(/29\.7000|-95\.3000/);
  });

  it("shows exceptions and delays, then active jobs", async () => {
    render(<LiveView />);
    const issues = await screen.findByRole("list", {
      name: "Exceptions and delays",
    });
    const rows = within(issues).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Failed");
    expect(rows[0]).toHaveTextContent("Site closed");
    expect(rows[1]).toHaveTextContent("+45 min");
    expect(rows[2]).toHaveTextContent("12% tank");
    const table = screen.getByRole("table", { name: "Active jobs" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByRole("link", { name: "J1" })).toHaveAttribute(
      "href",
      "/dashboard/dispatch/jobs/J1",
    );
  });

  it("Approvals tab is the inbox and ?id= focuses an item", async () => {
    mockParams = new URLSearchParams("tab=approvals&id=A2");
    render(<LiveView />);
    const row = await screen.findByTestId("approval-row-A2");
    await waitFor(() => expect(row).toHaveFocus());
    expect(row).toHaveAttribute("aria-current", "true");
    expect(screen.getByTestId("approval-row-A1")).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("Agents tab shows activity and health", async () => {
    mockParams = new URLSearchParams("tab=agents");
    render(<LiveView />);
    expect(screen.getByText("Agent activity feed")).toBeInTheDocument();
    expect(screen.getByText("Agent health panel")).toBeInTheDocument();
  });

  it("a failed source shows its own error with Retry", async () => {
    m(getActiveJobs).mockRejectedValue(new TypeError("Failed to fetch"));
    m(getDelayedJobs).mockResolvedValue({ data: [] });
    m(getFuelAlerts).mockResolvedValue({ data: [] });
    render(<LiveView />);
    expect(
      (
        await screen.findAllByText(
          "Can't reach Runsheet. Check your connection and retry.",
        )
      ).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("Failed to fetch")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Try again" }),
    ).toBeInTheDocument();
  });
});
