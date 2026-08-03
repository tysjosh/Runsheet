/**
 * Profile is the only door to password change.
 *
 * The Settings > Security tab used to render `<ChangePassword />` as well, so
 * there were two entry points onto one form. Security was removed as a
 * duplicate — which makes this component load-bearing in a way it was not
 * before: if `<ChangePassword />` ever stops rendering here, no user can change
 * their password anywhere in the web app, and nothing else would notice.
 *
 * `/dashboard/profile` is deliberately unregistered in `config/modules.ts` and
 * skipped by the route guard in `app/dashboard/layout.tsx`, and it is reached
 * from the Sidebar avatar rather than the role-filtered nav. So this must hold
 * for every role, including a driver.
 */

import { render, screen, waitFor } from "@testing-library/react";

const getAccountProfile = jest.fn();

jest.mock("../services/api", () => ({
  apiService: {
    get getAccountProfile() {
      return getAccountProfile;
    },
  },
}));

// The form itself is covered by its own suite; here we only care that Profile
// mounts it. A stub keeps this test from depending on the form's internals or
// its network calls.
jest.mock("./ChangePassword", () => ({
  __esModule: true,
  default: () => <div data-testid="change-password" />,
}));

import ProfilePage from "./ProfilePage";

describe("ProfilePage", () => {
  beforeEach(() => {
    getAccountProfile.mockReset();
    getAccountProfile.mockResolvedValue({
      email: "dispatcher@demo.runsheet.test",
      tenant_id: "demo-tenant",
      roles: ["dispatcher"],
      has_pii_access: false,
    });
  });

  it("renders the change-password form", async () => {
    render(<ProfilePage />);

    await waitFor(() => {
      expect(screen.getByTestId("change-password")).toBeInTheDocument();
    });
  });

  it("still renders it for a driver, who has no other route to it", async () => {
    getAccountProfile.mockResolvedValue({
      email: "mike.johnson@demo.runsheet.test",
      tenant_id: "demo-tenant",
      roles: ["driver"],
      has_pii_access: false,
    });

    render(<ProfilePage />);

    await waitFor(() => {
      expect(screen.getByTestId("change-password")).toBeInTheDocument();
    });
  });
});
