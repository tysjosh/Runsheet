/**
 * Cargo Tracking page load states (F-2): a missing job shows a not-found
 * state instead of an empty, editable manifest.
 */
import { fireEvent, render, screen } from "@testing-library/react";

const mockPush = jest.fn();
jest.mock("next/navigation", () => ({
  useParams: () => ({ id: "JOB-X" }),
  useRouter: () => ({ push: mockPush, back: jest.fn() }),
}));

jest.mock("../../../../../../services/schedulingApi", () => ({
  getJob: jest.fn(),
  getCargo: jest.fn(),
}));

jest.mock("../../../../../../hooks/useSchedulingWebSocket", () => ({
  useSchedulingWebSocket: jest.fn(),
}));

const mockEditor = jest.fn();
jest.mock("../../../../../../components/ops/CargoManifestEditor", () => ({
  __esModule: true,
  default: (props: { items: unknown[] }) => {
    mockEditor(props);
    return <div data-testid="cargo-editor">{props.items.length} items</div>;
  },
}));

import { ApiError } from "../../../../../../services/api";
import { getCargo, getJob } from "../../../../../../services/schedulingApi";
import CargoTrackingPage from "./page";

const mockGetJob = getJob as jest.MockedFunction<typeof getJob>;
const mockGetCargo = getCargo as jest.MockedFunction<typeof getCargo>;

describe("CargoTrackingPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    (console.error as jest.Mock).mockRestore();
  });

  it("shows a not-found state and no editor when the job doesn't exist", async () => {
    mockGetJob.mockRejectedValue(new ApiError("Job not found", 404));
    mockGetCargo.mockRejectedValue(new ApiError("Job not found", 404));

    render(<CargoTrackingPage />);

    expect(
      await screen.findByRole("heading", { name: "Job not found" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/job "JOB-X"/)).toBeInTheDocument();
    expect(screen.queryByText(/No cargo items/)).toBeNull();
    expect(screen.queryByRole("button", { name: /edit/i })).toBeNull();
    expect(screen.queryByTestId("cargo-editor")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Back to Job Board/ }));
    expect(mockPush).toHaveBeenCalledWith("/dashboard/dispatch?tab=jobs");
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("shows the error banner with retry on 500", async () => {
    mockGetJob.mockRejectedValue(new ApiError("boom", 500));
    mockGetCargo.mockResolvedValue({ data: [] } as any);

    render(<CargoTrackingPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(screen.queryByTestId("cargo-editor")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(mockGetJob).toHaveBeenCalledTimes(2);
  });

  it("renders the editor with the cargo items on success", async () => {
    const items = [{ item_id: "I-1" }, { item_id: "I-2" }];
    mockGetJob.mockResolvedValue({
      data: {
        job_id: "JOB-X",
        origin: "Depot",
        destination: "Site",
        asset_assigned: null,
        status: "scheduled",
      },
    } as any);
    mockGetCargo.mockResolvedValue({ data: items } as any);

    render(<CargoTrackingPage />);

    expect(await screen.findByTestId("cargo-editor")).toHaveTextContent(
      "2 items",
    );
    expect(mockEditor).toHaveBeenLastCalledWith(
      expect.objectContaining({ jobId: "JOB-X", items }),
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
