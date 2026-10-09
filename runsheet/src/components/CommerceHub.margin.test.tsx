/**
 * CommerceHub Margin tab gating (margin-feed AC-32, D1, and the Phase 3
 * owner-reported staging bug): a non-admin never sees the tab and
 * `?tab=margin` shows the standard no-access state; an admin sees it with
 * the open-alert badge only while the feed is on. With
 * COMMERCE_MARGIN_FEED_ENABLED off the margin routes answer 404
 * COMMERCE_DISABLED: the tab is hidden and `?tab=margin` says the feature
 * isn't turned on, with no Try again.
 */
import { act, render, screen, waitFor } from "@testing-library/react";

jest.mock("../utils/auth", () => ({
  ...jest.requireActual("../utils/auth"),
  getCurrentUserRoles: jest.fn(),
}));
jest.mock("../services/marginApi", () => ({
  getOpenMarginAlertCount: jest.fn(),
  getMarginAvailability: jest.fn(),
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

import {
  getMarginAvailability,
  getOpenMarginAlertCount,
} from "../services/marginApi";
import { getCurrentUserRoles } from "../utils/auth";
import CommerceHub from "./CommerceHub";

const mockRoles = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;
const mockCount = getOpenMarginAlertCount as jest.MockedFunction<
  typeof getOpenMarginAlertCount
>;

const mockAvailability = getMarginAvailability as jest.MockedFunction<
  typeof getMarginAvailability
>;

beforeEach(() => {
  jest.clearAllMocks();
  mockCount.mockResolvedValue(3);
  mockAvailability.mockResolvedValue("enabled");
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
    await screen.findByRole("tab", { name: "Invoices" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("tab", { name: /^Margin/ }),
  ).not.toBeInTheDocument();
  expect(mockCount).not.toHaveBeenCalled();
});

it("shows the standard no-access state for a dispatcher deep-linked to ?tab=margin", async () => {
  await renderAs(["dispatcher"], "margin");
  expect(
    await screen.findByRole("heading", {
      name: "You don't have access to this margin page",
    }),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/Ask an administrator for the admin role/),
  ).toBeInTheDocument();
  expect(screen.queryByText("Margin hub")).not.toBeInTheDocument();
  expect(screen.queryByText("Invoices page")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Try again/ })).toBeNull();
  expect(mockAvailability).not.toHaveBeenCalled();
});

it.each([
  [["platform_admin"]],
  [["driver"]],
  [["dispatcher", "platform_admin"]],
])("hides the Margin tab from %j", async (roles) => {
  await renderAs(roles, "margin");
  expect(
    screen.queryByRole("tab", { name: /^Margin/ }),
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
  expect(screen.getByRole("tab", { name: /^Margin/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(mockCount).toHaveBeenCalledTimes(1);
});

it("omits the badge when nothing is open", async () => {
  mockCount.mockResolvedValue(0);
  await renderAs(["admin"]);
  await waitFor(() => expect(mockCount).toHaveBeenCalled());
  expect(screen.getByRole("tab", { name: "Margin" })).toBeInTheDocument();
  expect(
    screen.queryByRole("img", { name: /open margin alert/ }),
  ).not.toBeInTheDocument();
});

describe("margin feed off (404 COMMERCE_DISABLED)", () => {
  beforeEach(() => mockAvailability.mockResolvedValue("disabled"));

  it("hides the Margin tab from an admin and skips the alert badge", async () => {
    await renderAs(["admin"]);
    await waitFor(() => expect(mockAvailability).toHaveBeenCalled());
    expect(
      await screen.findByRole("tab", { name: "Invoices" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /^Margin/ })).toBeNull();
    expect(mockCount).not.toHaveBeenCalled();
  });

  it("says Margin isn't turned on for ?tab=margin, with no Try again", async () => {
    await renderAs(["admin"], "margin");
    expect(
      await screen.findByRole("heading", {
        name: "Margin isn't enabled for your account",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Margin hub")).toBeNull();
    expect(screen.queryByRole("button", { name: /Try again/ })).toBeNull();
    expect(
      screen.getByRole("link", { name: "Go to Invoices" }),
    ).toHaveAttribute("href", "/dashboard/billing?tab=invoices");
  });
});

it("keeps the tab when the probe fails for another reason (the pages show the error)", async () => {
  mockAvailability.mockResolvedValue("unknown");
  await renderAs(["admin"], "margin");
  expect(await screen.findByText("Margin hub")).toBeInTheDocument();
});
