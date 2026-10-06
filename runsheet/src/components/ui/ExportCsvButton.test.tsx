/**
 * Tests for ExportCsvButton: role gate, accessible name, keyboard
 * activation, in-flight guard, and the inline status/error messages.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ApiError } from "../../services/api";
import { downloadCsvExport } from "../../services/exportApi";
import { getCurrentUserRoles } from "../../utils/auth";
import { EXPORT_MESSAGES, ExportCsvButton } from "./ExportCsvButton";

jest.mock("../../services/exportApi", () => ({
  downloadCsvExport: jest.fn(),
}));
jest.mock("../../utils/auth", () => ({
  getCurrentUserRoles: jest.fn(),
}));

const mockDownload = downloadCsvExport as jest.MockedFunction<
  typeof downloadCsvExport
>;
const mockRoles = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;

// Browsers compute "Export CSV: orders" (the sr-only span is inline). jsdom
// has no layout, so dom-accessibility-api treats the span as a block and
// inserts a space before it; the pattern accepts exactly that difference.
const NAME = /^Export CSV ?: orders$/;

async function clickAndSettle(element: HTMLElement) {
  await act(async () => {
    fireEvent.click(element);
  });
}

const FROM_SESSION = Symbol("from-session");

function renderButton(
  rolesArg: readonly string[] | null | typeof FROM_SESSION = ["dispatcher"],
) {
  const roles = rolesArg === FROM_SESSION ? undefined : rolesArg;
  return render(
    <ExportCsvButton
      type="orders"
      params={{ status: "placed" }}
      allowedRoles={["admin", "dispatcher"]}
      subject="orders"
      rolesOverride={roles}
    />,
  );
}

beforeEach(() => {
  mockDownload.mockReset();
  mockRoles.mockReset();
});

describe("ExportCsvButton role gate", () => {
  it("renders nothing while roles are unresolved", () => {
    renderButton(null);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders nothing for a role that is not allowed", () => {
    renderButton(["driver"]);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("resolves roles from the session when no override is given", async () => {
    mockRoles.mockResolvedValue(["admin"]);
    renderButton(FROM_SESSION);
    expect(
      await screen.findByRole("button", { name: NAME }),
    ).toBeInTheDocument();
    expect(mockRoles).toHaveBeenCalledTimes(1);
  });

  it("stays hidden when the session roles are not allowed", async () => {
    mockRoles.mockResolvedValue(["driver"]);
    renderButton(FROM_SESSION);
    await act(async () => {});
    expect(mockRoles).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("ExportCsvButton behavior", () => {
  it("is a native button named 'Export CSV: orders' with no aria-label", () => {
    renderButton();
    const button = screen.getByRole("button", { name: NAME });
    expect(button.tagName).toBe("BUTTON");
    expect(button).not.toHaveAttribute("aria-label");
    expect(button).toHaveTextContent("Export CSV");
  });

  it("calls downloadCsvExport with the type and params", async () => {
    mockDownload.mockResolvedValue({ filename: "x.csv" });
    renderButton();
    await clickAndSettle(screen.getByRole("button", { name: NAME }));
    expect(mockDownload).toHaveBeenCalledWith("orders", { status: "placed" });
    expect(await screen.findByRole("status")).toHaveTextContent(
      EXPORT_MESSAGES.success,
    );
  });

  it("is keyboard operable: a focusable native type=button", () => {
    // Enter and Space activation is native to <button>; jsdom does not
    // synthesize it, so assert the element that guarantees it.
    renderButton();
    const button = screen.getByRole("button", { name: NAME });
    expect(button).toHaveAttribute("type", "button");
    expect(button).not.toHaveAttribute("tabindex");
    button.focus();
    expect(button).toHaveFocus();
  });

  it("is aria-busy and disabled in flight, and a double click sends one request", async () => {
    let resolve: (v: { filename: string }) => void = () => {};
    mockDownload.mockImplementation(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    renderButton();
    const button = screen.getByRole("button", { name: NAME });
    act(() => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(mockDownload).toHaveBeenCalledTimes(1);
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toBeDisabled();
    await act(async () => resolve({ filename: "x.csv" }));
    expect(button).toHaveAttribute("aria-busy", "false");
    expect(button).not.toBeDisabled();
  });

  it.each([
    [413, EXPORT_MESSAGES.tooLarge],
    [429, EXPORT_MESSAGES.rateLimited],
    [403, EXPORT_MESSAGES.forbidden],
    [500, EXPORT_MESSAGES.generic],
    [0, EXPORT_MESSAGES.generic],
  ])("shows the %i message in role=alert", async (status, message) => {
    mockDownload.mockRejectedValue(new ApiError("x", status));
    renderButton();
    await clickAndSettle(screen.getByRole("button", { name: NAME }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
  });

  it("clears the previous error on the next click", async () => {
    mockDownload.mockRejectedValueOnce(new ApiError("x", 429));
    mockDownload.mockResolvedValueOnce({ filename: "x.csv" });
    renderButton();
    const button = screen.getByRole("button", { name: NAME });
    await clickAndSettle(button);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      EXPORT_MESSAGES.rateLimited,
    );
    await clickAndSettle(button);
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(""),
    );
  });
});
