/**
 * Analytics hub (UI revamp task 3.7): Ops Monitoring is retired from
 * Analytics for every role, platform_admin included (D12; poison-queue depth
 * lives in Settings → System health), and there is no time-range selector
 * (D13, OI-50).
 */
import { render, screen, waitFor } from "@testing-library/react";

jest.mock("../utils/auth", () => ({
  getCurrentUserRoles: jest.fn(),
}));
const mockReplace = jest.fn();
let mockSearch = "";
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: jest.fn() }),
  usePathname: () => "/dashboard/analytics",
  useSearchParams: () => new URLSearchParams(mockSearch),
}));
// The tab panels are lazy; none of them is opened by these tests.
jest.mock("./Analytics", () => ({ __esModule: true, default: () => null }));

import { getCurrentUserRoles } from "../utils/auth";
import AnalyticsHub from "./AnalyticsHub";

const rolesMock = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;

afterEach(() => {
  jest.clearAllMocks();
  mockSearch = "";
});

describe("AnalyticsHub", () => {
  it.each([
    [["dispatcher"]],
    [["driver"]],
    [["admin"]],
    [["platform_admin", "admin"]],
    [[]],
  ])("has no Ops Monitoring tab for roles %j", async (roles) => {
    rolesMock.mockResolvedValue(roles);
    render(<AnalyticsHub />);
    await waitFor(() => expect(rolesMock).toHaveBeenCalled());
    expect(
      await screen.findByRole("tab", { name: /Overview/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Ops monitoring/i)).not.toBeInTheDocument();
  });

  it("shows Overview, Scheduling metrics and Fleet efficiency only", async () => {
    rolesMock.mockResolvedValue(["admin"]);
    render(<AnalyticsHub />);
    await screen.findByRole("tab", { name: /Overview/ });
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "Overview",
      "Scheduling metrics",
      "Fleet efficiency",
    ]);
  });

  it("renders one h1 and no time-range selector", async () => {
    rolesMock.mockResolvedValue(["admin"]);
    render(<AnalyticsHub />);
    await waitFor(() => expect(rolesMock).toHaveBeenCalled());
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(
      screen.queryByRole("combobox", { name: /time range|range/i }),
    ).toBeNull();
    expect(screen.queryByText(/Last 7 days|Last 30 days/i)).toBeNull();
  });

  it("sends an old ?tab=ops-monitoring bookmark to System health for platform_admin", async () => {
    mockSearch = "tab=ops-monitoring";
    rolesMock.mockResolvedValue(["platform_admin", "admin"]);
    render(<AnalyticsHub />);
    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        "/dashboard/settings?tab=system",
      ),
    );
  });

  it.each([[["admin"]], [["dispatcher"]]])(
    "keeps %j on Overview for an old ?tab=ops-monitoring bookmark",
    async (roles) => {
      mockSearch = "tab=ops-monitoring";
      rolesMock.mockResolvedValue(roles);
      render(<AnalyticsHub />);
      expect(
        await screen.findByRole("tab", { name: /Overview/, selected: true }),
      ).toBeInTheDocument();
      expect(mockReplace).not.toHaveBeenCalledWith(
        "/dashboard/settings?tab=system",
      );
    },
  );
});
