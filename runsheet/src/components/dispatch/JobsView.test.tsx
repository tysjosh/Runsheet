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
  getDelayedJobs: jest.fn(),
  transitionStatus: jest.fn(),
}));
jest.mock("../../hooks/useSchedulingWebSocket", () => ({
  useSchedulingWebSocket: jest.fn(),
}));
jest.mock("../../utils/auth", () => ({
  ...jest.requireActual("../../utils/auth"),
  getCurrentUserRoles: jest.fn(async () => ["dispatcher"]),
}));

import {
  getDelayedJobs,
  getJobs,
  transitionStatus,
} from "../../services/schedulingApi";
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
  const rows = [
    job("J1", "scheduled"),
    job("J2", "in_progress"),
    job("J3", "in_progress", { delayed: true, delay_duration_minutes: 20 }),
    job("J4", "failed"),
  ];
  // A fake of the paged endpoint: `status` filters, `size: 1` is a count read.
  m(getJobs).mockImplementation(
    async (f: { status?: string; size?: number } = {}) => {
      const hit = rows.filter((r) => !f.status || r.status === f.status);
      return {
        data: f.size === 1 ? hit.slice(0, 1) : hit,
        pagination: { page: 1, size: f.size ?? 20, total: hit.length },
      };
    },
  );
  m(getDelayedJobs).mockResolvedValue({ data: [rows[2]] });
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
  await waitFor(() =>
    expect(
      within(chips).getByRole("button", { name: /^All/ }),
    ).toHaveTextContent("4"),
  );
  await waitFor(() =>
    expect(
      within(chips).getByRole("button", { name: /^In progress/ }),
    ).toHaveTextContent("2"),
  );
  fireEvent.click(within(chips).getByRole("button", { name: /^Delayed/ }));
  await waitFor(() => expect(getDelayedJobs).toHaveBeenCalled());
  await waitFor(() =>
    expect(
      within(screen.getByRole("table", { name: "Job board" })).getAllByRole(
        "row",
      ),
    ).toHaveLength(2),
  );
  expect(
    within(screen.getByRole("table", { name: "Job board" })).getAllByRole(
      "row",
    )[1],
  ).toHaveTextContent("J3");
});
it("with more than one page, chips filter on the server, count tenant totals and page", async () => {
  // 137 completed jobs in the tenant; the endpoint pages them.
  const many = Array.from({ length: 50 }, (_, i) => job(`C${i}`, "completed"));
  m(getJobs).mockImplementation(
    async (f: { status?: string; size?: number; page?: number } = {}) => {
      const totals: Record<string, number> = { completed: 137 };
      const total = f.status ? (totals[f.status] ?? 0) : 160;
      const data =
        f.status === "completed" || !f.status ? many.slice(0, f.size) : [];
      return { data, pagination: { page: f.page ?? 1, size: f.size, total } };
    },
  );
  renderHosted();
  await screen.findByRole("table", { name: "Job board" });
  const chips = screen.getByRole("group", { name: "Job status" });
  await waitFor(() =>
    expect(
      within(chips).getByRole("button", { name: /^Completed/ }),
    ).toHaveTextContent("137"),
  );
  expect(within(chips).getByRole("button", { name: /^All/ })).toHaveTextContent(
    "160",
  );
  fireEvent.click(within(chips).getByRole("button", { name: /^Completed/ }));
  await waitFor(() =>
    expect(getJobs).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "completed", page: 1, size: 50 }),
    ),
  );
  // 137 / 50 → three pages; the next page asks the server for page 2.
  fireEvent.click(await screen.findByRole("button", { name: /next/i }));
  await waitFor(() =>
    expect(getJobs).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "completed", page: 2, size: 50 }),
    ),
  );
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
