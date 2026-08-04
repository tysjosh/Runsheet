/**
 * Sidebar role filtering.
 *
 * The registry and `canSee` are unit-tested in `config/modules.test.ts`. What is
 * verified here is the wiring: that the sidebar actually resolves the session's
 * roles, filters `NAV_SECTIONS` through the predicate, and drops a section whose
 * items are all gone rather than leaving a bare heading behind.
 *
 * The unresolved-roles case is the one worth having a render test for. A gate
 * that is correct in isolation still leaks if the component renders once with
 * `[]`, once with the resolved roles, and shows the privileged item in between.
 */

import { render, screen, waitFor } from "@testing-library/react";
import Sidebar from "./Sidebar";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
}));

jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: { signOut: jest.fn() },
}));

// The profile fetch only fills in the footer email; keep it out of the way.
jest.mock("../services/api", () => ({
  apiService: { getAccountProfile: jest.fn().mockResolvedValue(null) },
}));

jest.mock("../utils/auth", () => ({
  getCurrentUserRoles: jest.fn(),
}));

import { getCurrentUserRoles } from "../utils/auth";

const rolesMock = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;

function renderSidebar() {
  return render(
    <Sidebar
      activeItem="today"
      isCollapsed={false}
      onToggle={() => {}}
      onNavigate={() => {}}
    />,
  );
}

const navItem = (label: string) =>
  screen.queryByRole("button", { name: label });

describe("Sidebar role filtering", () => {
  it("shows the Admin destination to an admin", async () => {
    rolesMock.mockResolvedValue(["admin"]);
    renderSidebar();
    await waitFor(() => expect(navItem("Admin")).toBeInTheDocument());
    // Sanity: the ordinary operational items are there too, so this is not
    // passing because everything happens to render.
    expect(navItem("Dispatch")).toBeInTheDocument();
  });

  it("hides the Admin destination from a dispatcher", async () => {
    rolesMock.mockResolvedValue(["dispatcher"]);
    renderSidebar();
    // Wait for a dispatcher-visible item so the assertion below is made after
    // roles resolved, not before.
    await waitFor(() => expect(navItem("Dispatch")).toBeInTheDocument());
    expect(navItem("Admin")).not.toBeInTheDocument();
  });

  it("shows no role-gated destination before roles resolve", async () => {
    // A promise that never settles models the window between mount and the
    // session's claims arriving.
    rolesMock.mockReturnValue(new Promise<string[]>(() => {}));
    renderSidebar();

    // Settings is deliberately ungated (it holds password change), so it is the
    // proof that "nothing rendered at all" is not why the rest is absent.
    expect(navItem("Settings")).toBeInTheDocument();
    for (const label of ["Admin", "Dispatch", "Today", "Billing", "Setup"]) {
      expect(navItem(label)).not.toBeInTheDocument();
    }
  });

  it("drops a section heading when all of its items are filtered out", async () => {
    // A driver-role account in the dispatcher/admin web app: every operational
    // and commerce destination is gated, so only Workspace/Settings survives and
    // the Operations and Commerce headings must not be left empty.
    rolesMock.mockResolvedValue(["driver"]);
    renderSidebar();

    await waitFor(() => expect(navItem("Settings")).toBeInTheDocument());
    expect(screen.queryByText("Operations")).not.toBeInTheDocument();
    expect(screen.queryByText("Commerce")).not.toBeInTheDocument();
    expect(screen.getByText("Workspace")).toBeInTheDocument();
  });

  it("does not treat platform_admin as admin", async () => {
    // Mirrors the backend: the staff role is additive and implies nothing, so a
    // staff account holding only `platform_admin` reaches nothing here either.
    rolesMock.mockResolvedValue(["platform_admin"]);
    renderSidebar();

    await waitFor(() => expect(navItem("Settings")).toBeInTheDocument());
    expect(navItem("Admin")).not.toBeInTheDocument();
    expect(navItem("Dispatch")).not.toBeInTheDocument();
  });
});
