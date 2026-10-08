/**
 * Home (R14.7, R14.8, R14.15, R14.16; AC 3, AC 6, AC 7): tanks ranked by level
 * status, the 12 % tank reads "Order now", capability notices sit in their
 * panels, each panel fails on its own, and no catalog code is shown.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: { attemptRefreshingSession: jest.fn() },
}));

import {
  errorBody,
  fakeResponse,
  invoice,
  ME,
  tank,
} from "../../components/portal/__fixtures__/portal";
import {
  ORDERING_UNAVAILABLE_MESSAGE,
  PAYMENTS_UNAVAILABLE_MESSAGE,
} from "../../components/portal/messages";
import { PortalMeContext } from "../../components/portal/PortalContext";
import type {
  PortalMe,
  PortalOrder,
  PortalTank,
} from "../../services/portalApi";
import PortalHomePage from "./page";

const fc = (days: number) => ({
  runout_at: "2026-10-20T12:00:00Z",
  days_to_runout: days,
  generated_at: "2026-10-08T12:00:00Z",
});

const TANKS: PortalTank[] = [
  tank({
    customer_tank_id: "QA-T-OK",
    label: "Tank …T-OK01",
    product_code: "HEATING_OIL",
    capacity_gallons: 1000,
    current_level_gallons: 820,
    percent_full: 82,
    reading_stale: true,
    forecast: null,
  }),
  tank({
    customer_tank_id: "QA-T-WARN",
    label: "North yard diesel",
    product_code: "DIESEL_2",
    capacity_gallons: 2000,
    current_level_gallons: 640,
    percent_full: 32,
    forecast: fc(4),
  }),
  tank({
    customer_tank_id: "QA-T-CRIT",
    label: "Farm off-road",
    product_code: "OFF_ROAD_DIESEL",
    capacity_gallons: 1500,
    current_level_gallons: 180,
    percent_full: 12,
    forecast: fc(1),
  }),
];

const ORDER: PortalOrder = {
  order_id: "ord_portal_abc",
  status_code: "out_for_delivery",
  status_label: "Out for delivery",
  product_code: "HEATING_OIL",
  gallons_requested: 400,
  fill_to_full: false,
  window_start: "2026-10-09T14:20:00Z",
  window_end: "2026-10-09T19:08:00Z",
  po_number: null,
  tank: { customer_tank_id: "QA-T-OK", label: "Tank …T-OK01" },
  created_at: "2026-10-08T12:00:00Z",
  delivered_at: null,
  delivered_gallons: null,
  ticket_number: null,
  cancellable: false,
};

type Route = { status?: number; body: unknown };

function install(routes: { tanks?: Route; orders?: Route; invoices?: Route }) {
  const calls: string[] = [];
  global.fetch = jest.fn(async (url: string) => {
    const u = String(url);
    calls.push(u);
    const pick = u.includes("/portal/tanks")
      ? routes.tanks
      : u.includes("/portal/orders")
        ? routes.orders
        : u.includes("/portal/invoices")
          ? routes.invoices
          : undefined;
    return fakeResponse(pick ?? { body: { data: [], next_cursor: null } });
  }) as unknown as typeof fetch;
  return calls;
}

const list = (data: unknown[]) => ({
  body: { data, next_cursor: null, request_id: "r" },
});

function renderHome(me: PortalMe = ME) {
  return render(
    <PortalMeContext.Provider value={me}>
      <PortalHomePage />
    </PortalMeContext.Provider>,
  );
}

describe("Home", () => {
  it("ranks tanks by level status and marks the 12 % tank Order now", async () => {
    install({
      tanks: list(TANKS),
      orders: list([ORDER]),
      invoices: list([invoice()]),
    });
    renderHome();
    const panel = await screen.findByRole("region", { name: "Your tanks" });
    await waitFor(() =>
      expect(within(panel).getAllByRole("meter")).toHaveLength(3),
    );
    const titles = within(panel)
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);
    expect(titles).toEqual([
      "Farm off-road",
      "North yard diesel",
      "Heating oil (No. 2) tank",
    ]);
    const crit = within(panel).getByRole("meter", { name: "Farm off-road" });
    expect(crit).toHaveAttribute("aria-valuetext", "180 of 1,500 gal, 12 %");
    expect(crit).toHaveAttribute("data-level", "critical");
    const critRow = crit.closest("article") as HTMLElement;
    expect(within(critRow).getByText("Order now")).toBeInTheDocument();
    expect(
      within(critRow).getByText(/12 % · empty in about 1 day$/),
    ).toBeInTheDocument();
    // No catalog code anywhere (AC 3), no "Tank Tank", no order id.
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/\b[A-Z0-9]+_[A-Z0-9_]+\b/);
    expect(text).not.toMatch(/Tank …|Tank Tank|ord_portal/);
  });

  it("shows the customer name nowhere on the page (it lives in the top bar)", async () => {
    install({ tanks: list(TANKS), orders: list([]), invoices: list([]) });
    renderHome();
    await screen.findByRole("region", { name: "Your tanks" });
    expect(screen.queryByText(ME.customer_display_name)).toBeNull();
    expect(screen.queryByText(/Signed in as/)).toBeNull();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("opens the request dialog with the tank selected from its row", async () => {
    install({ tanks: list(TANKS), orders: list([]), invoices: list([]) });
    renderHome();
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Request delivery for North yard diesel",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Request a delivery",
    });
    expect(within(dialog).getByLabelText("North yard diesel")).toBeChecked();
  });

  it("puts each capability notice in the panel it affects", async () => {
    install({
      tanks: list(TANKS),
      orders: list([ORDER]),
      invoices: list([invoice({ payable: true })]),
    });
    renderHome({ ...ME, ordering_available: false, payments_available: false });
    const tanksPanel = await screen.findByRole("region", {
      name: "Your tanks",
    });
    expect(
      within(tanksPanel).getByText(ORDERING_UNAVAILABLE_MESSAGE),
    ).toBeInTheDocument();
    const balance = screen.getByRole("region", { name: "Balance due" });
    expect(
      await within(balance).findByText(PAYMENTS_UNAVAILABLE_MESSAGE),
    ).toBeInTheDocument();
    expect(within(balance).queryByRole("link", { name: "Pay" })).toBeNull();
  });

  it("hides Balance due while invoicing is off", async () => {
    const calls = install({ tanks: list(TANKS), orders: list([]) });
    renderHome({ ...ME, invoices_available: false });
    await screen.findByRole("region", { name: "Your tanks" });
    expect(screen.queryByRole("region", { name: "Balance due" })).toBeNull();
    expect(calls.some((u) => u.includes("/portal/invoices"))).toBe(false);
  });

  it("uses the server balance (PE4) and sends Pay to the list when more invoices are open", async () => {
    install({
      tanks: list(TANKS),
      orders: list([]),
      // Only one payable invoice comes back in the capped window …
      invoices: list([invoice({ invoice_id: "QA-INV-9", payable: true })]),
    });
    // … but the server says two are open.
    renderHome({
      ...ME,
      open_balance_cents: 526600,
      open_invoice_count: 2,
      overdue_count: 1,
    });
    const balance = await screen.findByRole("region", { name: "Balance due" });
    expect(await within(balance).findByText("$5,266.00")).toBeInTheDocument();
    expect(
      within(balance).getByText("2 invoices · 1 overdue"),
    ).toBeInTheDocument();
    expect(within(balance).queryByText(/At least/)).toBeNull();
    expect(
      await within(balance).findByRole("link", { name: "View and pay" }),
    ).toHaveAttribute("href", "/portal/invoices");
    expect(within(balance).queryByRole("link", { name: "Pay" })).toBeNull();
  });

  it("links Pay straight to the invoice when it is the only open one", async () => {
    install({
      tanks: list(TANKS),
      orders: list([]),
      invoices: list([invoice({ invoice_id: "QA-INV-9", payable: true })]),
    });
    renderHome({
      ...ME,
      open_balance_cents: 367447,
      open_invoice_count: 1,
      overdue_count: 0,
    });
    const balance = await screen.findByRole("region", { name: "Balance due" });
    expect(
      await within(balance).findByRole("link", { name: "Pay" }),
    ).toHaveAttribute("href", "/portal/invoices/QA-INV-9/pay");
  });

  it("asks for active orders only (PE7) and shows them", async () => {
    const calls = install({
      tanks: list(TANKS),
      orders: list([ORDER]),
      invoices: list([]),
    });
    renderHome();
    const orders = await screen.findByRole("region", { name: "Active orders" });
    expect(
      await within(orders).findByText("Out for delivery"),
    ).toBeInTheDocument();
    expect(calls.find((u) => u.includes("/portal/orders"))).toMatch(
      /status_group=active/,
    );
  });

  it("fails one panel at a time, each with Retry", async () => {
    install({
      tanks: { status: 500, body: errorBody("INTERNAL_ERROR", "boom") },
      orders: list([ORDER]),
      invoices: list([invoice()]),
    });
    renderHome();
    const tanksPanel = await screen.findByRole("region", {
      name: "Your tanks",
    });
    expect(await within(tanksPanel).findByRole("alert")).toHaveTextContent(
      "We couldn't load your tanks.",
    );
    expect(
      within(tanksPanel).getByRole("button", { name: "Retry" }),
    ).toBeInTheDocument();
    const orders = screen.getByRole("region", { name: "Active orders" });
    expect(
      await within(orders).findByText("Out for delivery"),
    ).toBeInTheDocument();
  });
});
