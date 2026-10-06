/**
 * Export CSV on the scheduling job board: role gate and current filters.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

jest.mock("../../../services/schedulingApi", () => ({
  getJobs: jest.fn(async () => ({ data: [], request_id: "r" })),
  transitionStatus: jest.fn(),
}));
jest.mock("../../../hooks/useSchedulingWebSocket", () => ({
  useSchedulingWebSocket: jest.fn(() => ({ isConnected: true })),
}));
jest.mock("../../../services/exportApi", () => ({
  downloadCsvExport: jest.fn(),
}));
jest.mock("../../../utils/auth", () => ({
  ...jest.requireActual("../../../utils/auth"),
  getCurrentUserRoles: jest.fn(async () => []),
}));

import { downloadCsvExport } from "../../../services/exportApi";
import { getJobs } from "../../../services/schedulingApi";
import { getCurrentUserRoles } from "../../../utils/auth";
import SchedulingJobBoardPage from "./page";

const mockDownload = downloadCsvExport as jest.MockedFunction<
  typeof downloadCsvExport
>;
const mockRoles = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;
const mockGetJobs = getJobs as jest.MockedFunction<typeof getJobs>;

beforeEach(() => {
  mockDownload.mockReset();
  mockDownload.mockResolvedValue({ filename: "jobs.csv" });
});

it("is hidden for a driver", async () => {
  mockRoles.mockResolvedValue(["driver"]);
  render(<SchedulingJobBoardPage />);
  await waitFor(() => expect(mockRoles).toHaveBeenCalled());
  await act(async () => {});
  expect(
    screen.queryByRole("button", { name: /Export CSV/ }),
  ).not.toBeInTheDocument();
});

it("exports the current filters for a dispatcher", async () => {
  mockRoles.mockResolvedValue(["dispatcher"]);
  render(<SchedulingJobBoardPage />);
  await screen.findByRole("button", { name: /^Export CSV ?: jobs$/ });
  fireEvent.change(screen.getByLabelText("Start date"), {
    target: { value: "2026-10-01" },
  });
  await waitFor(() =>
    expect(mockGetJobs).toHaveBeenLastCalledWith(
      expect.objectContaining({ start_date: "2026-10-01" }),
    ),
  );
  // The board re-renders while it reloads, so query the button again.
  const button = await screen.findByRole("button", {
    name: /^Export CSV ?: jobs$/,
  });
  await act(async () => {
    fireEvent.click(button);
  });
  expect(mockDownload).toHaveBeenCalledWith("jobs", {
    start_date: "2026-10-01",
  });
});
