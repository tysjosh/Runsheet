/**
 * CommerceHub Margin tab gating (margin-feed AC-32, D1): a dispatcher never
 * sees the tab and `?tab=margin` falls back to the default tab; an admin sees
 * it with the aria-labelled open-alert badge. Presentation only: the API
 * refuses non-admins on every margin route.
 */
import { act, render, screen, waitFor } from "@testing-library/react";

jest.mock("../utils/auth", () => ({
  ...jest.requireActual("../utils/auth"),
  getCurrentUserRoles: jest.fn(),
}));
jest.mock("../services/marginApi", () => ({
  getOpenMarginAlertCount: jest.fn(),
}));
// The hub's pages are stubbed: this test is about which tab is offered.
jest.mock("./commerce/InvoicesListPage", () => ({
  __esModule: true,
  default: () => <div>Invoices page</div>,
}));
jest.mock("./commerce/AccountsListPage", () => ({
  __esModule: true,
  default: () => <div>Accounts page</div>,
}));
jest.mock("./commerce/margin/MarginHub", () => ({
  __esModule: true,
  default: () => <div>Margin hub</div>,
}));
jest.mock("./ops/ReconciliationPage", () => ({
  __esModule: true,
  default: () => <div>Reconciliation page</div>,
}));

import { getOpenMarginAlertCount } from "../services/marginApi";
import { getCurrentUserRoles } from "../utils/auth";
import CommerceHub from "./CommerceHub";

const mockRoles = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;
const mockCount = getOpenMarginAlertCount as jest.MockedFunction<
  typeof getOpenMarginAlertCount
>;

beforeEach(() => {
  jest.clearAllMocks();
  mockCount.mockResolvedValue(3);
});

async function renderAs(roles: string[], initialTab?: string) {
  mockRoles.mockResolvedValue(roles);
  await act(async () => {
    render(<CommerceHub initialTab={initialTab} />);
  });
  await waitFor(() => expect(mockRoles).toHaveBeenCalled());
}

it("hides the Margin tab from a dispatcher and never asks for alert counts", async () => {
  await renderAs(["dispatcher"]);
  expect(
    await screen.findByRole("button", { name: "Invoices" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /^Margin/ }),
  ).not.toBeInTheDocument();
  expect(mockCount).not.toHaveBeenCalled();
});

it("falls back to the default tab for a dispatcher deep-linked to ?tab=margin", async () => {
  await renderAs(["dispatcher"], "margin");
  expect(await screen.findByText("Invoices page")).toBeInTheDocument();
  expect(screen.queryByText("Margin hub")).not.toBeInTheDocument();
});

it.each([
  [["platform_admin"]],
  [["driver"]],
  [["dispatcher", "platform_admin"]],
])("hides the Margin tab from %j", async (roles) => {
  await renderAs(roles, "margin");
  expect(
    screen.queryByRole("button", { name: /^Margin/ }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText("Margin hub")).not.toBeInTheDocument();
  expect(mockCount).not.toHaveBeenCalled();
});

it("shows the Margin tab to an admin with the open-alert badge", async () => {
  await renderAs(["admin"], "margin");
  expect(await screen.findByText("Margin hub")).toBeInTheDocument();
  const badge = await screen.findByRole("img", {
    name: "3 open margin alerts",
  });
  expect(badge).toHaveTextContent("3");
  expect(screen.getByRole("button", { name: /^Margin/ })).toHaveAttribute(
    "aria-current",
    "page",
  );
  expect(mockCount).toHaveBeenCalledTimes(1);
});

it("omits the badge when nothing is open", async () => {
  mockCount.mockResolvedValue(0);
  await renderAs(["admin"]);
  await waitFor(() => expect(mockCount).toHaveBeenCalled());
  expect(screen.getByRole("button", { name: "Margin" })).toBeInTheDocument();
  expect(
    screen.queryByRole("img", { name: /open margin alert/ }),
  ).not.toBeInTheDocument();
});
