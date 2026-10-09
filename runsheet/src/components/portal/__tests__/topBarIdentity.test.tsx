/**
 * portal-fixes A2/A3: the avatar shows the signed-in person's initials (the
 * staff IdentityAvatar rule), not the customer account's, and the names block
 * is not capped at a fixed width.
 */
import { render, screen } from "@testing-library/react";
import { initials } from "../../../lib/identity";
import { avatarLabel } from "../PortalAccountMenu";
import PortalShell from "../PortalShell";

jest.mock("next/navigation", () => ({
  usePathname: () => "/portal/orders",
  useRouter: () => ({ replace: jest.fn(), push: jest.fn() }),
}));
jest.mock("../../../utils/auth", () => ({ signOut: jest.fn() }));

describe("top bar identity", () => {
  it("initials come from the email's local part", () => {
    expect(initials(avatarLabel("olukotunjosh@gmail.com"))).toBe("OL");
    expect(initials(avatarLabel("jane.doe@example.test"))).toBe("JD");
    expect(initials(avatarLabel("ap-team@example.test"))).toBe("AT");
  });

  it("renders the user's initials and full, untruncated-by-default names", () => {
    render(
      <PortalShell
        supplierName="Demo Fuels"
        customerName="QA-OWNER Demo Fuels Customer"
        email="olukotunjosh@gmail.com"
        invoicesAvailable
      >
        <p>content</p>
      </PortalShell>,
    );
    const avatar = screen.getByRole("img", { name: "olukotunjosh" });
    expect(avatar).toHaveTextContent("OL");
    const customer = screen.getByText("QA-OWNER Demo Fuels Customer");
    expect(customer).toHaveAttribute("title", "QA-OWNER Demo Fuels Customer");
    expect(customer.parentElement?.className).not.toMatch(/max-w-\[240px\]/);
    expect(screen.getByText("Demo Fuels")).toBeInTheDocument();
    expect(screen.queryByText("demo-tenant")).toBeNull();
  });
});
