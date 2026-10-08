/**
 * The Ops Monitoring tab is `platform_admin` only (UI revamp task 0.2): the
 * poison-queue route behind it 403s for every tenant role.
 */
import { render, screen, waitFor } from "@testing-library/react";

jest.mock("../utils/auth", () => ({
  getCurrentUserRoles: jest.fn(),
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
});

describe("AnalyticsHub Ops Monitoring tab", () => {
  it.each([[["dispatcher"]], [["driver"]], [["admin"]], [[]]])(
    "is hidden for roles %j",
    async (roles) => {
      rolesMock.mockResolvedValue(roles);
      render(<AnalyticsHub />);
      await waitFor(() => expect(rolesMock).toHaveBeenCalled());
      expect(screen.getByText("Overview")).toBeInTheDocument();
      expect(screen.queryByText(/Ops monitoring/i)).not.toBeInTheDocument();
    },
  );

  it("is shown for platform_admin", async () => {
    rolesMock.mockResolvedValue(["platform_admin", "admin"]);
    render(<AnalyticsHub />);
    expect(
      await screen.findByRole("tab", { name: /Ops monitoring/i }),
    ).toBeInTheDocument();
  });
});
