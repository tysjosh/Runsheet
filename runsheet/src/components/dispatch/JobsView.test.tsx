/**
 * Dispatch → Jobs (UI revamp task 2.4, R8.6): no header of its own (Create
 * job goes to the Dispatch title row), status chips with counts replace the
 * summary bar, `?status=` picks a chip, rows open the job route, transition
 * errors surface as a toast.
 */
import "@testing-library/jest-dom";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

const mockPush = jest.fn();
let mockParams = new URLSearchParams();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: jest.fn() }),
  useSearchParams: () => mockParams,
}));
jest.mock("../../services/schedulingApi", () => ({
  getJobs: jest.fn(),
  transitionStatus: jest.fn(),
}));
jest.mock("../../hooks/useSchedulingWebSocket", () => ({
  useSchedulingWebSocket: jest.fn(),
}));
jest.mock("../../utils/auth", () => ({
  ...jest.requireActual("../../utils/auth"),
  getCurrentUserRoles: jest.fn(async () => ["dispatcher"]),
}));

import { getJobs, transitionStatus } from "../../services/schedulingApi";
import { PageChromeProvider, PageHeader } from "../ui";
import { GlobalToaster } from "../ui/toast/notify";
import JobsView from "./JobsView";

const m = <T,>(f: T) => f as unknown as jest.Mock;
const job = (
  id: string,
  status: string,
  extra: Record<string, unknown> = {},
) => ({
  job_id: id,
  job_type: "fuel_delivery",
  status,
  tenant_id: "t",
  origin: "Terminal",
  destination: `Site ${id}`,
  scheduled_time: "2026-10-08T13:00:00Z",
  created_at: "2026-10-08T10:00:00Z",
  updated_at: "2026-10-08T10:00:00Z",
  priority: "normal",
  delayed: false,
  ...extra,
});

beforeEach(() => {
  jest.clearAllMocks();
  mockParams = new URLSearchParams();
  m(getJobs).mockResolvedValue({
    data: [
      job("J1", "scheduled"),
      job("J2", "in_progress"),
      job("J3", "in_progress", { delayed: true, delay_duration_minutes: 20 }),
      job("J4", "failed"),
    ],
  });
});

function renderHosted() {
  return render(
    <PageChromeProvider>
      <PageHeader host title="Dispatch" />
      <JobsView />
      <GlobalToaster />
    </PageChromeProvider>,
  );
}

it("has no header of its own and puts Create job in the title row", async () => {
  const { container } = renderHosted();
  await screen.findByRole("table", { name: "Job board" });
  expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  const title = container.querySelector(
    '[data-chrome="titlerow"]',
  ) as HTMLElement;
  expect(
    within(title).getByRole("button", { name: "Create job" }),
  ).toBeInTheDocument();
});

it("status chips carry counts and filter the list", async () => {
  renderHosted();
  await screen.findByRole("table", { name: "Job board" });
  const chips = screen.getByRole("group", { name: "Job status" });
  expect(within(chips).getByRole("button", { name: /^All/ })).toHaveTextContent(
    "4",
  );
  expect(
    within(chips).getByRole("button", { name: /^In progress/ }),
  ).toHaveTextContent("2");
  fireEvent.click(within(chips).getByRole("button", { name: /^Delayed/ }));
  const rows = within(
    screen.getByRole("table", { name: "Job board" }),
  ).getAllByRole("row");
  expect(rows).toHaveLength(2);
  expect(rows[1]).toHaveTextContent("J3");
});

it("?status=delayed (the Dashboard's link) opens on the Delayed chip", async () => {
  mockParams = new URLSearchParams("tab=jobs&status=delayed");
  renderHosted();
  await screen.findByRole("table", { name: "Job board" });
  expect(screen.getByRole("button", { name: /^Delayed/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

it("a row opens the job route", async () => {
  renderHosted();
  const table = await screen.findByRole("table", { name: "Job board" });
  fireEvent.click(within(table).getByText("Site J2"));
  expect(mockPush).toHaveBeenCalledWith("/dashboard/dispatch/jobs/J2");
});

it("a failed transition shows a toast instead of failing silently", async () => {
  m(transitionStatus).mockRejectedValue(new Error("Invalid transition"));
  renderHosted();
  await screen.findByRole("table", { name: "Job board" });
  fireEvent.click(screen.getByRole("button", { name: "Actions for job J2" }));
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name: "Complete" }));
  });
  await waitFor(() =>
    expect(screen.getByText("Job J2: Invalid transition")).toBeInTheDocument(),
  );
});

it("cargo search lives in the overflow and opens in a drawer", async () => {
  renderHosted();
  await screen.findByRole("table", { name: "Job board" });
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Cargo search" }));
  expect(
    await screen.findByRole("dialog", { name: "Cargo search" }),
  ).toBeInTheDocument();
});
