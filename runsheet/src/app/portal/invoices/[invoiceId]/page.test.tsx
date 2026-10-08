/**
 * Invoice detail (R14.13, AC 10, D28): Balance due first; Pay replaced by the
 * payment status while an attempt is in flight; the sticky Pay bar below
 * 640 px; line items as product chips at the stored unit price (PE3).
 */
import { render, screen, within } from "@testing-library/react";

jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: { attemptRefreshingSession: jest.fn() },
}));
jest.mock("next/navigation", () => ({
  __esModule: true,
  useParams: () => ({ invoiceId: "QA-INV-1" }),
  useRouter: () => ({ replace: jest.fn(), push: jest.fn() }),
}));

import {
  fakeResponse,
  invoice,
  ME,
} from "../../../../components/portal/__fixtures__/portal";
import { PAYMENTS_UNAVAILABLE_MESSAGE } from "../../../../components/portal/messages";
import { PortalMeContext } from "../../../../components/portal/PortalContext";
import type { PortalInvoice, PortalMe } from "../../../../services/portalApi";
import Page from "./page";

function renderWith(inv: PortalInvoice, me: PortalMe = ME) {
  global.fetch = jest.fn(async () =>
    fakeResponse({ body: { data: inv, request_id: "r" } }),
  ) as unknown as typeof fetch;
  return render(
    <PortalMeContext.Provider value={me}>
      <Page />
    </PortalMeContext.Provider>,
  );
}

function phone(on: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: on && query.includes("max-width: 639.98px"),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
}
const realMatchMedia = window.matchMedia;
afterEach(() => {
  window.matchMedia = realMatchMedia;
});

describe("invoice detail", () => {
  it("leads with Balance due and Pay on a payable invoice; sticky bar on phones", async () => {
    phone(true);
    renderWith(invoice());
    const card = await screen.findByRole("region", { name: "Balance due" });
    expect(within(card).getByText("$1,050.00")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Pay" })).toHaveAttribute(
      "href",
      "/portal/invoices/QA-INV-1/pay",
    );
    expect(
      screen.getByRole("link", { name: "Pay $1,050.00" }),
    ).toBeInTheDocument();
    // Balance due is the first content after the title row.
    expect(card).toHaveAttribute("data-portal-first");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Invoice 1001",
    );
  });

  it("has no sticky bar from 640 px", async () => {
    phone(false);
    renderWith(invoice());
    await screen.findByRole("region", { name: "Balance due" });
    expect(screen.queryByRole("link", { name: /^Pay \$/ })).toBeNull();
  });

  it("replaces Pay with the payment status while an attempt is pending", async () => {
    phone(true);
    renderWith(
      invoice({
        payment_attempt: {
          payment_attempt_id: "QA-PA-1",
          status_code: "pending",
          status_label: "Payment processing",
          amount_cents: 105000,
          created_at: "2026-10-08T12:00:00Z",
        },
      }),
    );
    const card = await screen.findByRole("region", { name: "Balance due" });
    expect(within(card).getByText("Payment processing")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^Pay/ })).toBeNull();
  });

  it("shows the PD15 text instead of Pay while payments are off", async () => {
    renderWith(invoice(), { ...ME, payments_available: false });
    const card = await screen.findByRole("region", { name: "Balance due" });
    expect(
      within(card).getByText(PAYMENTS_UNAVAILABLE_MESSAGE),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^Pay/ })).toBeNull();
  });

  it("lists line items as product chips at the stored unit price", async () => {
    renderWith(
      invoice({
        line_items: [
          {
            product_code: "DIESEL_2",
            quantity_gallons: 1187.4,
            unit_price_cents: 292,
            unit_price_micros: 2_919_300,
            subtotal_cents: 346648,
          },
        ],
      }),
    );
    const lines = await screen.findByRole("region", { name: "Line items" });
    expect(within(lines).getByText("Diesel #2 (on-road)")).toBeInTheDocument();
    expect(
      within(lines).getByText("1,187.4 gal × $2.9193"),
    ).toBeInTheDocument();
    expect(within(lines).getByText("$3,466.48")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\b[A-Z0-9]+_[A-Z0-9_]+\b/);
  });
});
