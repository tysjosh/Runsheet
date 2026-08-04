/**
 * Tests for <AgentHealth> — the agent status panel and its pause/resume control.
 *
 * The behaviour being pinned: pausing an agent is a tenant-wide lifecycle change,
 * so the backend restricts it to `admin` via `agent_admin_dependency`
 * (`Agents/api_authz.py`). This panel renders inside `OperationsControlView` and
 * `/ops/command`, which are both `admin` + `dispatcher` surfaces — so a
 * dispatcher sees the panel. They must not see a pause button that can only
 * return 403.
 *
 * The status list stays visible to dispatchers; only the control is gated. This
 * is presentation, not enforcement — the backend re-checks regardless — but a
 * button that always fails is a defect in its own right.
 */

import "@testing-library/jest-dom";
import { render, screen, waitFor } from "@testing-library/react";

jest.mock("../../services/agentApi", () => ({
  getAgentHealth: jest.fn(),
  pauseAgent: jest.fn(),
  resumeAgent: jest.fn(),
}));
jest.mock("../../utils/auth", () => ({
  getCurrentUserRoles: jest.fn(),
}));

import { getAgentHealth } from "../../services/agentApi";
import { getCurrentUserRoles } from "../../utils/auth";
import AgentHealth from "./AgentHealth";

const mockGetAgentHealth = getAgentHealth as jest.MockedFunction<
  typeof getAgentHealth
>;
const mockGetCurrentUserRoles = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;

function healthFixture() {
  return {
    agents: {
      delay_response_agent: {
        agent_id: "delay_response_agent",
        status: "running",
        type: "autonomous",
      },
      sla_guardian_agent: {
        agent_id: "sla_guardian_agent",
        status: "stopped",
        type: "autonomous",
      },
    },
  } as unknown as Awaited<ReturnType<typeof getAgentHealth>>;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGetAgentHealth.mockResolvedValue(healthFixture());
});

describe("AgentHealth — pause/resume is admin-only", () => {
  it("offers pause and resume to an admin", async () => {
    mockGetCurrentUserRoles.mockResolvedValue(["admin"]);

    render(<AgentHealth />);

    expect(
      await screen.findByRole("button", { name: /pause delay response/i }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /resume sla guardian/i }),
    ).toBeInTheDocument();
  });

  it("hides the control from a dispatcher but still shows agent status", async () => {
    mockGetCurrentUserRoles.mockResolvedValue(["dispatcher"]);

    render(<AgentHealth />);

    // The panel is still useful: a dispatcher supervises agent state during a
    // shift, they just cannot change the lifecycle.
    expect(await screen.findByText("Delay Response")).toBeInTheDocument();
    expect(screen.getByText("SLA Guardian")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();

    expect(
      screen.queryByRole("button", { name: /pause/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /resume/i }),
    ).not.toBeInTheDocument();
  });

  it("hides the control from a driver", async () => {
    mockGetCurrentUserRoles.mockResolvedValue(["driver"]);

    render(<AgentHealth />);

    await screen.findByText("Delay Response");
    expect(
      screen.queryByRole("button", { name: /pause/i }),
    ).not.toBeInTheDocument();
  });

  it("does not flash the control before roles resolve", async () => {
    // `getCurrentUserRoles` is async. If the component defaulted to admin, a
    // dispatcher would briefly see an actionable button on every mount.
    let release: (roles: string[]) => void = () => {};
    mockGetCurrentUserRoles.mockReturnValue(
      new Promise<string[]>((resolve) => {
        release = resolve;
      }) as ReturnType<typeof getCurrentUserRoles>,
    );

    render(<AgentHealth />);

    // Agents have loaded, roles have not.
    expect(await screen.findByText("Delay Response")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /pause/i }),
    ).not.toBeInTheDocument();

    release(["admin"]);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /pause delay response/i }),
      ).toBeInTheDocument(),
    );
  });

  it("treats a role whose name merely contains admin as non-admin", async () => {
    // Mirrors the backend's exact-match rule: no substring promotion.
    mockGetCurrentUserRoles.mockResolvedValue(["admin_readonly"]);

    render(<AgentHealth />);

    await screen.findByText("Delay Response");
    expect(
      screen.queryByRole("button", { name: /pause/i }),
    ).not.toBeInTheDocument();
  });
});
