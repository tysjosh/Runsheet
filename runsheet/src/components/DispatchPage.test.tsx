/**
 * Dispatch page Board tab (dispatch-board R1.1–R1.3, K13 kill switch).
 * The board API and the heavy tabs are mocked.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

const mockReplace = jest.fn();
let mockParams = new URLSearchParams();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: jest.fn() }),
  useSearchParams: () => mockParams,
}));
jest.mock("../services/dispatchBoardApi", () => {
  const actual = jest.requireActual("../services/dispatchBoardApi");
  return { ...actual, getBoardStatus: jest.fn() };
});
jest.mock("../app/ops/scheduling/page", () => ({
  __esModule: true,
  default: () => <div>Scheduling body</div>,
}));
jest.mock("./ops/FuelDistributionPage", () => ({
  __esModule: true,
  default: () => <div>Distribution body</div>,
}));
jest.mock("./dispatch-board/DispatchBoard", () => ({
  __esModule: true,
  default: ({ mode }: { mode: string }) => <div>Board body {mode}</div>,
}));

import { BoardApiError, getBoardStatus } from "../services/dispatchBoardApi";
import DispatchPage, { BOARD_STATUS_POLL_MS } from "./DispatchPage";

const mockStatus = getBoardStatus as jest.MockedFunction<typeof getBoardStatus>;

function tabNames() {
  return screen
    .getAllByRole("button")
    .filter((b) => b.closest("nav[aria-label='Tabs']"))
    .map((b) => b.textContent);
}

beforeEach(() => {
  mockParams = new URLSearchParams();
  mockReplace.mockReset();
  mockStatus.mockReset();
});

describe("Board tab visibility", () => {
  it.each([
    [404, "DISPATCH_BOARD_DISABLED"],
    [403, "FORBIDDEN"],
  ])("a %s from /status hides the tab", async (status, code) => {
    mockStatus.mockRejectedValue(new BoardApiError("no", status, code));
    render(<DispatchPage />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(await screen.findByText("Scheduling body")).toBeInTheDocument();
    expect(tabNames()).toEqual(["Scheduling", "Fuel Distribution"]);
  });

  it.each(["active_gated", "active_auto"] as const)(
    "%s shows Board first and makes it the default",
    async (mode) => {
      mockStatus.mockResolvedValue({ mode });
      render(<DispatchPage />);
      expect(await screen.findByText(`Board body ${mode}`)).toBeInTheDocument();
      expect(tabNames()).toEqual(["Board", "Scheduling", "Fuel Distribution"]);
      expect(screen.getByRole("button", { name: "Board" })).toHaveAttribute(
        "aria-current",
        "page",
      );
    },
  );

  it("shadow shows the tab but keeps Scheduling as the default", async () => {
    mockStatus.mockResolvedValue({ mode: "shadow" });
    render(<DispatchPage />);
    await screen.findByRole("button", { name: "Board" });
    expect(screen.getByText("Scheduling body")).toBeInTheDocument();
  });
});

describe("?tab= deep link", () => {
  it("?tab=board opens the board in shadow mode", async () => {
    mockParams = new URLSearchParams("tab=board&date=2026-10-08");
    mockStatus.mockResolvedValue({ mode: "shadow" });
    render(<DispatchPage />);
    expect(await screen.findByText("Board body shadow")).toBeInTheDocument();
  });

  it("?tab=distribution wins over the active default", async () => {
    mockParams = new URLSearchParams("tab=distribution");
    mockStatus.mockResolvedValue({ mode: "active_gated" });
    render(<DispatchPage />);
    await screen.findByRole("button", { name: "Board" });
    expect(screen.getByText("Distribution body")).toBeInTheDocument();
  });

  it("?tab=board on a disabled board falls back to Scheduling", async () => {
    mockParams = new URLSearchParams("tab=board");
    mockStatus.mockRejectedValue(
      new BoardApiError("no", 404, "DISPATCH_BOARD_DISABLED"),
    );
    render(<DispatchPage />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(await screen.findByText("Scheduling body")).toBeInTheDocument();
  });

  it("changing tab writes ?tab= and keeps other params", async () => {
    mockParams = new URLSearchParams("date=2026-10-08");
    mockStatus.mockResolvedValue({ mode: "active_gated" });
    render(<DispatchPage />);
    await screen.findByText("Board body active_gated");
    fireEvent.click(screen.getByRole("button", { name: "Fuel Distribution" }));
    expect(await screen.findByText("Distribution body")).toBeInTheDocument();
    expect(mockReplace).toHaveBeenCalledWith(
      "?date=2026-10-08&tab=distribution",
      { scroll: false },
    );
  });
});

describe("kill switch", () => {
  afterEach(() => jest.useRealTimers());

  it("a later 404 from the 5-minute poll hides the board and leaves the tab", async () => {
    jest.useFakeTimers();
    mockStatus.mockResolvedValueOnce({ mode: "active_gated" });
    render(<DispatchPage />);
    expect(
      await screen.findByText("Board body active_gated"),
    ).toBeInTheDocument();
    mockStatus.mockRejectedValueOnce(
      new BoardApiError("off", 404, "DISPATCH_BOARD_DISABLED"),
    );
    await act(async () => {
      jest.advanceTimersByTime(BOARD_STATUS_POLL_MS);
    });
    expect(await screen.findByText("Scheduling body")).toBeInTheDocument();
    expect(tabNames()).toEqual(["Scheduling", "Fuel Distribution"]);
  });

  it("re-reads /status on window focus; a transient error keeps the tab", async () => {
    mockStatus.mockResolvedValueOnce({ mode: "active_gated" });
    render(<DispatchPage />);
    await screen.findByText("Board body active_gated");
    mockStatus.mockRejectedValueOnce(
      new BoardApiError("down", 503, "DISPATCH_BOARD_MODE_UNAVAILABLE"),
    );
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(mockStatus).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Board body active_gated")).toBeInTheDocument();
  });
});
