/**
 * Sidebar (R2.2, R2.6): order, role filtering, the collapsible Back office
 * group and its remembered state, the pinned Settings entry, count badges.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { BACKOFFICE_STORAGE_KEY } from "../../config/nav";
import Sidebar from "./Sidebar";

function renderSidebar(
  roles: readonly string[] | null,
  extra: Partial<React.ComponentProps<typeof Sidebar>> = {},
) {
  return render(
    <Sidebar
      activeItem="today"
      roles={roles}
      isCollapsed={false}
      onToggle={() => {}}
      onNavigate={() => {}}
      {...extra}
    />,
  );
}

const item = (label: string | RegExp) =>
  screen.queryByRole("button", { name: label });

beforeEach(() => window.localStorage.clear());

describe("Sidebar", () => {
  it("lists the working set first, in order, and pins Settings", () => {
    renderSidebar(["admin"]);
    const primary = screen.getByRole("navigation", { name: "Primary" });
    const labels = within(primary)
      .getAllByRole("button")
      .map((b) => b.textContent?.trim());
    expect(labels).toEqual([
      "Dashboard",
      "Dispatch",
      "Orders",
      "Live",
      "Fleet",
      "Customers",
      "Fuel",
      "Back office",
      "Billing",
      "Compliance",
      "Analytics",
    ]);
    const pinned = screen.getByRole("navigation", { name: "Settings" });
    expect(
      within(pinned).getByRole("button", { name: "Settings" }),
    ).toBeInTheDocument();
  });

  it("puts the brand mark in the sidebar, not an h1", () => {
    const { container } = renderSidebar(["admin"]);
    expect(screen.getByText("Runsheet")).toBeInTheDocument();
    expect(container.querySelector("h1")).toBeNull();
  });

  it("collapses Back office by default for a dispatcher and remembers the choice", () => {
    const { unmount } = renderSidebar(["dispatcher"]);
    const toggle = screen.getByRole("button", { name: "Back office" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(item("Billing")).toBeNull();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(item("Billing")).toBeInTheDocument();
    expect(window.localStorage.getItem(BACKOFFICE_STORAGE_KEY)).toBe("open");
    unmount();
    renderSidebar(["dispatcher"]);
    expect(screen.getByRole("button", { name: "Back office" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("opens Back office by default for an admin, and while one of its pages is active", () => {
    const { unmount } = renderSidebar(["admin"]);
    expect(screen.getByRole("button", { name: "Back office" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    unmount();
    renderSidebar(["dispatcher"], { activeItem: "billing" });
    expect(item("Billing")).toHaveAttribute("aria-current", "page");
  });

  it("shows nothing role-gated before roles resolve, and nothing to a driver", () => {
    const { unmount } = renderSidebar(null);
    for (const label of ["Dashboard", "Dispatch", "Billing", "Settings"]) {
      expect(item(label)).toBeNull();
    }
    unmount();
    renderSidebar(["driver"]);
    expect(item("Dispatch")).toBeNull();
    expect(screen.queryByText("Back office")).toBeNull();
  });

  it("does not treat platform_admin as admin", () => {
    renderSidebar(["platform_admin"]);
    expect(item("Dispatch")).toBeNull();
    expect(item("Settings")).toBeNull();
  });

  it("shows count badges on Orders and Live", () => {
    renderSidebar(["dispatcher"], { counts: { orders: 7, live: 3 } });
    expect(item(/^Orders/)).toHaveTextContent("7 orders waiting");
    expect(item(/^Live/)).toHaveTextContent("3 need attention");
  });

  it("names icon-only items when the rail is collapsed", () => {
    renderSidebar(["dispatcher"], { isCollapsed: true, counts: { orders: 2 } });
    expect(
      screen.getByRole("button", { name: "Orders, 2" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Dispatch" }),
    ).toBeInTheDocument();
  });

  it("navigates and marks the active item", () => {
    const onNavigate = jest.fn();
    renderSidebar(["dispatcher"], { activeItem: "live", onNavigate });
    expect(item("Live")).toHaveAttribute("aria-current", "page");
    fireEvent.click(item("Fleet") as HTMLElement);
    expect(onNavigate).toHaveBeenCalledWith("fleet");
  });

  it("makes the nav the scrollable flex region with Settings below it", () => {
    renderSidebar(["admin"]);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(nav).toHaveClass("flex-1", "min-h-0", "overflow-y-auto");
    const pinned = screen.getByRole("navigation", { name: "Settings" });
    expect(
      nav.compareDocumentPosition(pinned) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
