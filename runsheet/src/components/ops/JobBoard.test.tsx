/**
 * JobBoard (Dispatch → Jobs list, UI revamp R8.6): DataTable with status
 * badges (icon + label, no row tinting), formatted dates, sortable headers
 * with `aria-sort` on the `th`, and the status transitions in a row menu. The
 * menu sits in the last column, so it stays reachable when the table scrolls
 * horizontally (OI-49).
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { Job } from "../../types/api";
import JobBoard from "./JobBoard";

const job: Job = {
  job_id: "JOB-1",
  job_type: "passenger_transport",
  status: "in_progress",
  tenant_id: "t1",
  origin: "Depot A",
  destination: "Site B",
  scheduled_time: "2026-10-05T10:00:00Z",
  created_at: "2026-10-05T09:00:00Z",
  updated_at: "2026-10-05T09:00:00Z",
  priority: "normal",
  delayed: false,
};

describe("JobBoard", () => {
  it("renders status as a badge and no status row tint (R5.5)", () => {
    render(
      <JobBoard
        jobs={[
          job,
          {
            ...job,
            job_id: "JOB-2",
            delayed: true,
            delay_duration_minutes: 25,
          },
        ]}
        onTransition={jest.fn()}
      />,
    );
    const table = screen.getByRole("table", { name: "Job board" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("In progress");
    expect(rows[1]).toHaveTextContent("Delayed +25 min");
    for (const r of rows)
      expect(r.className).not.toMatch(/bg-(warning|info|success|error)-light/);
    expect(
      within(table).getByRole("columnheader", { name: /Scheduled/ }),
    ).toHaveAttribute("aria-sort", "ascending");
  });

  it("offers only valid transitions in the row menu and runs them", async () => {
    const onTransition = jest.fn().mockResolvedValue(undefined);
    render(<JobBoard jobs={[job]} onTransition={onTransition} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Actions for job JOB-1" }),
    );
    const menu = screen.getByRole("menu");
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["Complete", "Fail…", "Cancel"]);
    await act(async () => {
      fireEvent.click(within(menu).getByRole("menuitem", { name: "Complete" }));
    });
    expect(onTransition).toHaveBeenCalledWith("JOB-1", "completed");
  });

  it("Fail asks for a reason in a dialog first", async () => {
    const onTransition = jest.fn().mockResolvedValue(undefined);
    render(<JobBoard jobs={[job]} onTransition={onTransition} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Actions for job JOB-1" }),
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Fail…" }));
    const dialog = screen.getByRole("dialog", {
      name: "Mark job JOB-1 as failed",
    });
    await act(async () => {
      fireEvent.click(
        within(dialog).getByRole("button", { name: "Mark failed" }),
      );
    });
    expect(onTransition).not.toHaveBeenCalled();
    expect(within(dialog).getByText("Enter a reason.")).toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText(/Failure reason/), {
      target: { value: "Site closed" },
    });
    await act(async () => {
      fireEvent.click(
        within(dialog).getByRole("button", { name: "Mark failed" }),
      );
    });
    expect(onTransition).toHaveBeenCalledWith("JOB-1", "failed", "Site closed");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("sorts by a header", () => {
    render(
      <JobBoard
        jobs={[job, { ...job, job_id: "JOB-0", origin: "Aardvark" }]}
        onTransition={jest.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Origin/ }));
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("JOB-0");
  });
});
