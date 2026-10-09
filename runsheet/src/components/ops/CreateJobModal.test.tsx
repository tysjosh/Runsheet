/**
 * Create job on FormDialog (design.md §5 "Job create", Phase 3 review
 * finding 3): inline validation, the payload, and the parts-risk warning on
 * the shared toaster (it outlives the closed dialog).
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/schedulingApi", () => ({ createJob: jest.fn() }));
jest.mock("../../services/inventoryApi", () => ({
  getAssetReadiness: jest.fn(),
}));
jest.mock("./AssetPicker", () => ({
  __esModule: true,
  default: ({ onChange }: { onChange: (id: string) => void }) => (
    <button type="button" onClick={() => onChange("QA-TRK-1")}>
      Pick QA-TRK-1
    </button>
  ),
}));

import { createJob } from "../../services/schedulingApi";
import { GlobalToaster, resetToasts } from "../ui/toast/notify";
import CreateJobModal, { riskMessage } from "./CreateJobModal";

const mockCreate = createJob as jest.Mock;

beforeEach(() => {
  jest.clearAllMocks();
  resetToasts();
});

function renderModal() {
  const onClose = jest.fn();
  const onCreated = jest.fn();
  render(
    <>
      <CreateJobModal onClose={onClose} onCreated={onCreated} />
      <GlobalToaster />
    </>,
  );
  return { onClose, onCreated };
}

it("validates required fields inline and does not submit", async () => {
  renderModal();
  const dialog = screen.getByRole("dialog", { name: "Create job" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Create job" }));
  expect(await screen.findByText("Enter the origin.")).toBeInTheDocument();
  expect(screen.getByText("Enter the destination.")).toBeInTheDocument();
  expect(screen.getByText("Choose the scheduled time.")).toBeInTheDocument();
  expect(mockCreate).not.toHaveBeenCalled();
});

it("creates the job, closes, and shows a parts-risk warning on the shared toaster", async () => {
  mockCreate.mockResolvedValue({
    data: {
      job_id: "QA-JOB-1",
      readiness_flags: {
        missing_parts: [{ name: "Hose" }],
        low_parts: [],
        depot_location: "QA Depot",
      },
    },
  });
  const { onClose, onCreated } = renderModal();
  const dialog = screen.getByRole("dialog", { name: "Create job" });
  fireEvent.change(screen.getByLabelText(/^Origin/), {
    target: { value: "QA Terminal" },
  });
  fireEvent.change(screen.getByLabelText(/^Destination/), {
    target: { value: "QA Site" },
  });
  fireEvent.change(screen.getByLabelText(/^Scheduled time/), {
    target: { value: "2026-10-09T08:30" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Pick QA-TRK-1" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Create job" }));
  await waitFor(() =>
    expect(mockCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        job_type: "cargo_transport",
        origin: "QA Terminal",
        destination: "QA Site",
        asset_assigned: "QA-TRK-1",
        priority: "normal",
      }),
    ),
  );
  await waitFor(() => expect(onCreated).toHaveBeenCalled());
  expect(onClose).toHaveBeenCalled();
  expect(
    await screen.findByText("Assignment risk at QA Depot: Out of stock: Hose"),
  ).toBeInTheDocument();
});

it("keeps the dialog open with the API error", async () => {
  mockCreate.mockRejectedValue(new Error("Asset not available"));
  const { onClose } = renderModal();
  fireEvent.change(screen.getByLabelText(/^Origin/), {
    target: { value: "A" },
  });
  fireEvent.change(screen.getByLabelText(/^Destination/), {
    target: { value: "B" },
  });
  fireEvent.change(screen.getByLabelText(/^Scheduled time/), {
    target: { value: "2026-10-09T08:30" },
  });
  fireEvent.click(
    within(screen.getByRole("dialog", { name: "Create job" })).getByRole(
      "button",
      { name: "Create job" },
    ),
  );
  expect(await screen.findByText("Asset not available")).toBeInTheDocument();
  expect(onClose).not.toHaveBeenCalled();
});

it("riskMessage is null without flags", () => {
  expect(riskMessage({})).toBeNull();
  expect(
    riskMessage({ readiness_flags: { missing_parts: [], low_parts: [] } }),
  ).toBeNull();
});
