/**
 * JobBoard: the Actions column stays pinned to the right edge (OI-49), so
 * Reject/Cancel stay visible when the table scrolls horizontally. jsdom has
 * no layout engine, so this asserts the sticky classes rather than geometry.
 */
import { render, screen, within } from "@testing-library/react";
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

describe("JobBoard Actions column", () => {
  it("pins the Actions header and cells to the right with an opaque background", () => {
    render(
      <JobBoard
        jobs={[job]}
        onTransition={jest.fn().mockResolvedValue(undefined)}
      />,
    );
    const table = screen.getByRole("table", { name: "Job board" });

    const header = within(table).getByRole("columnheader", { name: "Actions" });
    expect(header).toHaveClass("sticky", "right-0", "bg-gray-50");

    const rows = within(table).getAllByRole("row");
    const cells = within(rows[1]).getAllByRole("cell");
    const actionsCell = cells[cells.length - 1];
    expect(actionsCell).toHaveClass("sticky", "right-0", "bg-white");
    expect(within(actionsCell).getAllByRole("button").length).toBeGreaterThan(
      0,
    );
  });
});
