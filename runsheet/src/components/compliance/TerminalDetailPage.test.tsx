/**
 * Load-failure states for TerminalDetailPage (F-1).
 */
import { fireEvent, render, screen } from "@testing-library/react";

const mockBack = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), back: mockBack }),
}));

jest.mock("../../services/fuelApi", () => ({
  getTerminal: jest.fn(),
  getTerminalWaitSummary: jest.fn(),
}));

import { ApiError } from "../../services/api";
import { getTerminal, getTerminalWaitSummary } from "../../services/fuelApi";
import TerminalDetailPage from "./TerminalDetailPage";

const mockGetTerminal = getTerminal as jest.MockedFunction<typeof getTerminal>;
const mockGetWait = getTerminalWaitSummary as jest.MockedFunction<
  typeof getTerminalWaitSummary
>;

const terminal = {
  terminal_id: "TERM-1",
  name: "Houston Rack",
  operator: "Acme",
  status: "active",
  branded: false,
  location_lat: 29.7,
  location_lon: -95.3,
  supported_products: ["ULSD"],
};

describe("TerminalDetailPage load failures", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetWait.mockRejectedValue(new Error("no wait data"));
  });

  it("shows not found with a working back button on 404", async () => {
    mockGetTerminal.mockRejectedValue(
      new ApiError("Terminal not found", 404, "terminal_not_found"),
    );

    render(<TerminalDetailPage terminalId="TERM-missing" />);

    expect(
      await screen.findByRole("heading", { name: "Terminal not found" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/terminal "TERM-missing"/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Go back/ }));
    expect(mockBack).toHaveBeenCalled();
    expect(
      screen.getByRole("link", { name: "Go to Compliance" }),
    ).toHaveAttribute("href", "/dashboard/compliance");
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("uses the in-shell onBack when provided", async () => {
    const onBack = jest.fn();
    mockGetTerminal.mockRejectedValue(new ApiError("nope", 404));

    render(<TerminalDetailPage terminalId="TERM-missing" onBack={onBack} />);

    fireEvent.click(await screen.findByRole("button", { name: /Go back/ }));
    expect(onBack).toHaveBeenCalled();
    expect(mockBack).not.toHaveBeenCalled();
  });

  it("shows the error banner and retries on 500", async () => {
    mockGetTerminal.mockRejectedValueOnce(new ApiError("boom", 500));
    mockGetTerminal.mockResolvedValueOnce(terminal as any);

    render(<TerminalDetailPage terminalId="TERM-1" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("heading", { name: "Houston Rack" }),
    ).toBeInTheDocument();
    expect(mockGetTerminal).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });
});
