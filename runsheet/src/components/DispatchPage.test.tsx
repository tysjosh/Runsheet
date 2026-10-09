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

// The router writes the URL the way Next.js does; tests re-render to read it.
const mockReplace = jest.fn((url: string) => {
  mockParams = new URLSearchParams(url.split("?")[1] ?? "");
});
let mockParams = new URLSearchParams();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: jest.fn() }),
  usePathname: () => "/dashboard/dispatch",
  useSearchParams: () => mockParams,
}));
jest.mock("../services/dispatchBoardApi", () => {
  const actual = jest.requireActual("../services/dispatchBoardApi");
  return { ...actual, getBoardStatus: jest.fn() };
});
jest.mock("./dispatch/JobsView", () => ({
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
  return screen.getAllByRole("tab").map((b) => b.textContent);
}

beforeEach(() => {
  mockParams = new URLSearchParams();
  mockReplace.mockClear();
  mockStatus.mockReset();
});

describe("Dispatch title row", () => {
  it("renders one h1 with Board · Jobs · Plans inline", async () => {
    mockStatus.mockResolvedValue({ mode: "active_gated" });
    const { container } = render(<DispatchPage />);
    await screen.findByText("Board body active_gated");
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    expect(
      screen.getByRole("tablist", { name: "Dispatch views" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Board");
  });
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
    expect(tabNames()).toEqual(["Jobs", "Plans"]);
  });

  it.each(["active_gated", "active_auto"] as const)(
    "%s shows Board first and makes it the default",
    async (mode) => {
      mockStatus.mockResolvedValue({ mode });
      render(<DispatchPage />);
      expect(await screen.findByText(`Board body ${mode}`)).toBeInTheDocument();
      expect(tabNames()).toEqual(["Board", "Jobs", "Plans"]);
      expect(screen.getByRole("tab", { name: "Board" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    },
  );

  it("shadow shows the tab but keeps Jobs as the default", async () => {
    mockStatus.mockResolvedValue({ mode: "shadow" });
    render(<DispatchPage />);
    await screen.findByRole("tab", { name: "Board" });
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

  it("?tab=distribution (legacy alias of plans) wins over the active default", async () => {
    mockParams = new URLSearchParams("tab=distribution");
    mockStatus.mockResolvedValue({ mode: "active_gated" });
    render(<DispatchPage />);
    await screen.findByRole("tab", { name: "Board" });
    expect(screen.getByText("Distribution body")).toBeInTheDocument();
  });

  it("?tab=scheduling (legacy alias of jobs) opens Jobs", async () => {
    mockParams = new URLSearchParams("tab=scheduling");
    mockStatus.mockResolvedValue({ mode: "active_gated" });
    render(<DispatchPage />);
    await screen.findByRole("tab", { name: "Board" });
    expect(screen.getByText("Scheduling body")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Jobs" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("?tab=board on a disabled board falls back to Jobs", async () => {
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
    const { rerender } = render(<DispatchPage />);
    await screen.findByText("Board body active_gated");
    fireEvent.click(screen.getByRole("tab", { name: "Plans" }));
    expect(mockReplace).toHaveBeenCalledWith(
      "/dashboard/dispatch?date=2026-10-08&tab=plans",
      { scroll: false },
    );
    rerender(<DispatchPage />);
    expect(await screen.findByText("Distribution body")).toBeInTheDocument();
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
    expect(tabNames()).toEqual(["Jobs", "Plans"]);
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
