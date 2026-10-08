/**
 * UI-4: staff Portal access panel (PD24, design §10.3) and its placement on
 * the customer detail page (admin only).
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), back: jest.fn() }),
}));

jest.mock("../../../services/portalApi", () => ({
  listPortalUsers: jest.fn(),
  invitePortalUser: jest.fn(),
  resendPortalUserLink: jest.fn(),
  revokePortalUser: jest.fn(),
}));

jest.mock("../../../services/commerceApi", () => ({
  getCustomer: jest.fn(),
}));

jest.mock("../../../utils/auth", () => ({
  getCurrentUserRoles: jest.fn(),
}));

import { ApiError } from "../../../services/api";
import { getCustomer } from "../../../services/commerceApi";
import {
  invitePortalUser,
  listPortalUsers,
  resendPortalUserLink,
  revokePortalUser,
} from "../../../services/portalApi";
import { getCurrentUserRoles } from "../../../utils/auth";
import CustomerDetailPage from "../CustomerDetailPage";
import PortalAccessPanel from "../PortalAccessPanel";

const mockList = listPortalUsers as jest.MockedFunction<typeof listPortalUsers>;
const mockInvite = invitePortalUser as jest.MockedFunction<
  typeof invitePortalUser
>;
const mockResend = resendPortalUserLink as jest.MockedFunction<
  typeof resendPortalUserLink
>;
const mockRevoke = revokePortalUser as jest.MockedFunction<
  typeof revokePortalUser
>;
const mockRoles = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;
const mockGetCustomer = getCustomer as jest.MockedFunction<typeof getCustomer>;

const USER = {
  grant_id: "pug_1",
  email: "ap@example.test",
  status: "active" as const,
  created_at: "2026-10-01T12:00:00Z",
  revoked_at: null,
};

const LINK = "https://staging.example/auth/reset-password?token=QA-TOKEN";

beforeEach(() => {
  jest.clearAllMocks();
  Object.assign(navigator, {
    clipboard: { writeText: jest.fn().mockResolvedValue(undefined) },
  });
});

describe("PortalAccessPanel", () => {
  it("hides itself when the portal is off (404)", async () => {
    mockList.mockRejectedValue(
      new ApiError("Not found", 404, "PORTAL_DISABLED"),
    );
    const { container } = render(<PortalAccessPanel customerId="QA-CUST-A" />);
    await waitFor(() => expect(mockList).toHaveBeenCalledWith("QA-CUST-A"));
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("lists users with their status", async () => {
    mockList.mockResolvedValue({
      data: [
        USER,
        {
          ...USER,
          grant_id: "pug_2",
          email: "old@example.test",
          status: "revoked",
        },
      ],
    });
    render(<PortalAccessPanel customerId="QA-CUST-A" />);
    const table = await screen.findByRole("table", { name: "Portal users" });
    expect(within(table).getByText("ap@example.test")).toBeInTheDocument();
    expect(within(table).getByText("Active")).toBeInTheDocument();
    expect(within(table).getByText("Revoked")).toBeInTheDocument();
    // A revoked grant has no actions.
    expect(
      within(table).queryByRole("button", {
        name: "Revoke access for old@example.test",
      }),
    ).toBeNull();
  });

  it("shows the invite link in a read-only field with Copy", async () => {
    mockList.mockResolvedValue({ data: [] });
    mockInvite.mockResolvedValue({
      grant_id: "pug_9",
      email: "new@example.test",
      status: "invited",
      password_set_link: LINK,
      link_error: false,
      email_sent: true,
      already_invited: false,
    });
    render(<PortalAccessPanel customerId="QA-CUST-A" />);
    await screen.findByText("No portal users yet.");
    fireEvent.change(screen.getByLabelText("Invite by email"), {
      target: { value: "new@example.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));

    const field = await screen.findByLabelText(
      "Password-set link for new@example.test",
    );
    expect(field).toHaveValue(LINK);
    expect(field).toHaveAttribute("readonly");
    expect(mockInvite).toHaveBeenCalledWith("QA-CUST-A", "new@example.test");
    expect(screen.getByText(/The link was also emailed/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(LINK),
    );
    expect(
      await screen.findByRole("button", { name: "Copied" }),
    ).toBeInTheDocument();
    expect(mockList).toHaveBeenCalledTimes(2);
  });

  it("shows the server's refusal for an email in use", async () => {
    mockList.mockResolvedValue({ data: [] });
    mockInvite.mockRejectedValue(
      new ApiError(
        "This email can't be invited to this customer's portal.",
        409,
        "PORTAL_EMAIL_IN_USE",
      ),
    );
    render(<PortalAccessPanel customerId="QA-CUST-A" />);
    await screen.findByText("No portal users yet.");
    fireEvent.change(screen.getByLabelText("Invite by email"), {
      target: { value: "staff@example.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This email can't be invited",
    );
  });

  it("resends a link after confirming in the dialog", async () => {
    mockList.mockResolvedValue({ data: [USER] });
    mockResend.mockResolvedValue({
      password_set_link: LINK,
      link_error: false,
      email_sent: false,
    });
    render(<PortalAccessPanel customerId="QA-CUST-A" />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Resend link to ap@example.test",
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Resend the password-set link?",
    });
    expect(mockResend).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Resend" }));
    await waitFor(() =>
      expect(mockResend).toHaveBeenCalledWith("QA-CUST-A", "pug_1"),
    );
    expect(
      await screen.findByLabelText("Password-set link for ap@example.test"),
    ).toHaveValue(LINK);
  });

  it("revokes after confirming, and cancel does nothing", async () => {
    mockList.mockResolvedValue({ data: [USER] });
    mockRevoke.mockResolvedValue({ grant_id: "pug_1", status: "revoked" });
    render(<PortalAccessPanel customerId="QA-CUST-A" />);
    const revoke = await screen.findByRole("button", {
      name: "Revoke access for ap@example.test",
    });

    fireEvent.click(revoke);
    let dialog = screen.getByRole("dialog", { name: "Revoke portal access?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(mockRevoke).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(revoke);
    dialog = screen.getByRole("dialog", { name: "Revoke portal access?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() =>
      expect(mockRevoke).toHaveBeenCalledWith("QA-CUST-A", "pug_1"),
    );
    expect(
      await screen.findByText("Portal access for ap@example.test was revoked."),
    ).toBeInTheDocument();
    expect(mockList).toHaveBeenCalledTimes(2);
  });
});

describe("CustomerDetailPage placement", () => {
  const CUSTOMER = {
    customer_id: "QA-CUST-A",
    tenant_id: "demo-tenant",
    display_name: "QA Customer A",
    legal_name: null,
    primary_email: null,
    tax_id: null,
    status: "active" as const,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    external_refs: {},
    metadata: {},
    open_invoice_count: 0,
    open_balance_cents: 0,
    lifetime_revenue_cents: 0,
    account_count: 1,
  };

  beforeEach(() => {
    mockGetCustomer.mockResolvedValue({ data: CUSTOMER, request_id: "r" });
    mockList.mockResolvedValue({ data: [] });
  });

  it("shows the panel to an admin", async () => {
    mockRoles.mockResolvedValue(["admin"]);
    render(<CustomerDetailPage customerId="QA-CUST-A" />);
    expect(
      await screen.findByRole("heading", { name: "Portal access" }),
    ).toBeInTheDocument();
  });

  it.each([["dispatcher"], ["platform_admin"], ["driver"]])(
    "hides the panel from %s",
    async (role) => {
      mockRoles.mockResolvedValue([role]);
      render(<CustomerDetailPage customerId="QA-CUST-A" />);
      await screen.findByRole("heading", { name: "QA Customer A" });
      await waitFor(() => expect(mockRoles).toHaveBeenCalled());
      expect(
        screen.queryByRole("heading", { name: "Portal access" }),
      ).toBeNull();
      expect(mockList).not.toHaveBeenCalled();
    },
  );
});
