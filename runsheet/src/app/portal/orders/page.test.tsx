/**
 * Customer Cancel on the portal orders page (R4.10, PD8; review R2, D-F6-2).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: { attemptRefreshingSession: jest.fn() },
}));

import {
  errorBody,
  type FakeResponseInit,
  fakeResponse,
  ME,
} from "../../../components/portal/__fixtures__/portal";
import {
  REQUEST_CHANGED_MESSAGE,
  requestCancelledMessage,
} from "../../../components/portal/messages";
import { PortalMeContext } from "../../../components/portal/PortalContext";
import { date } from "../../../components/portal/portalFormat";
import type { PortalOrder } from "../../../services/portalApi";
import PortalOrdersPage from "./page";

function order(overrides: Partial<PortalOrder> = {}): PortalOrder {
  return {
    order_id: "QA-ORD-1",
    status_code: "awaiting_confirmation",
    status_label: "Awaiting confirmation",
    product_code: "ULSD",
    gallons_requested: 300,
    fill_to_full: false,
    window_start: "2026-10-12T13:00:00Z",
    window_end: "2026-10-12T17:00:00Z",
    po_number: null,
    tank: { customer_tank_id: "QA-TANK-1", label: "North yard" },
    created_at: "2026-10-08T12:00:00Z",
    delivered_at: null,
    delivered_gallons: null,
    ticket_number: null,
    cancellable: true,
    ...overrides,
  };
}

function listBody(orders: PortalOrder[]) {
  return { data: orders, next_cursor: null, request_id: "req-test" };
}

const CANCELLED = order({
  status_code: "cancelled",
  status_label: "Cancelled",
  cancellable: false,
});
const CONFIRMED = order({
  status_code: "confirmed",
  status_label: "Confirmed",
  cancellable: false,
});

type Calls = Array<{ url: string; method: string }>;

/**
 * `global.fetch` answering list GETs with `lists` in order (the last repeats)
 * and the cancel POST with `cancel`, which may be a pending promise.
 */
function installOrdersFetch(
  lists: PortalOrder[][],
  cancel: FakeResponseInit | Promise<FakeResponseInit> = {},
): Calls {
  const calls: Calls = [];
  const queue = [...lists];
  global.fetch = jest.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ url: String(url), method });
    if (method === "POST") return fakeResponse(await cancel);
    // The page also loads the tanks (titles and the request dialog).
    if (String(url).includes("/portal/tanks")) {
      return fakeResponse({ body: { data: [], next_cursor: null } });
    }
    const next = queue.length > 1 ? queue.shift() : queue[0];
    return fakeResponse({ body: listBody(next ?? []) });
  }) as unknown as typeof fetch;
  return calls;
}

function renderPage() {
  return render(
    <PortalMeContext.Provider value={ME}>
      <PortalOrdersPage />
    </PortalMeContext.Provider>,
  );
}

/** D29: the tank and the date, never the order id. */
const REFERENCE = `North yard, ${date("2026-10-12T13:00:00Z")}`;
const CANCEL_NAME = `Cancel request for ${REFERENCE}`;

function cancelButton() {
  return screen.getByRole("button", { name: CANCEL_NAME });
}

const orderGets = (calls: Calls) =>
  calls.filter((c) => c.method === "GET" && /\/portal\/orders/.test(c.url));

function row(button: HTMLElement): HTMLElement {
  const li = button.closest("li");
  if (!li) throw new Error("button is not inside a row");
  return li;
}

describe("customer Cancel request", () => {
  it("shows Cancel only on cancellable rows", async () => {
    installOrdersFetch([
      [order(), order({ ...CONFIRMED, order_id: "QA-ORD-2" })],
    ]);
    renderPage();
    expect(await screen.findByText("Confirmed")).toBeInTheDocument();
    expect(cancelButton()).toBeInTheDocument();
    // No accessible name carries an internal order id.
    for (const b of screen.getAllByRole("button")) {
      expect(b.getAttribute("aria-label") ?? "").not.toMatch(/QA-ORD|ord_/);
    }
    expect(
      screen.getAllByRole("button", { name: /^Cancel request/ }),
    ).toHaveLength(1);
  });

  it("cancels, disables while in flight, announces, reloads and keeps focus on the row", async () => {
    let finish: (value: FakeResponseInit) => void = () => {};
    const pending = new Promise<FakeResponseInit>((resolve) => {
      finish = resolve;
    });
    const calls = installOrdersFetch([[order()], [CANCELLED]], pending);
    renderPage();
    const button = await screen.findByRole("button", { name: CANCEL_NAME });
    const li = row(button);
    button.focus();
    fireEvent.click(button);
    await waitFor(() =>
      expect(button).toHaveAttribute("aria-disabled", "true"),
    );
    // A second click while in flight sends nothing.
    fireEvent.click(button);
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
    expect(calls.find((c) => c.method === "POST")?.url).toMatch(
      /\/portal\/orders\/QA-ORD-1\/cancel$/,
    );

    finish({ body: { data: CANCELLED, request_id: "req-test" } });

    const status = await screen.findByText(requestCancelledMessage(REFERENCE));
    expect(status.textContent).not.toMatch(/QA-ORD/);
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(await screen.findByText("Cancelled")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Cancel request/ }),
    ).toBeNull();
    expect(orderGets(calls)).toHaveLength(2);
    // The row stayed mounted (in-place reload) and has focus.
    await waitFor(() => expect(li).toHaveFocus());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reloads on 409 ORDER_NOT_CANCELLABLE and announces the change", async () => {
    const calls = installOrdersFetch([[order()], [CONFIRMED]], {
      status: 409,
      body: errorBody("ORDER_NOT_CANCELLABLE", "Order can't be cancelled"),
    });
    renderPage();
    const button = await screen.findByRole("button", { name: CANCEL_NAME });
    const li = row(button);
    fireEvent.click(button);

    const status = await screen.findByText(REQUEST_CHANGED_MESSAGE);
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(await screen.findByText("Confirmed")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Cancel request/ }),
    ).toBeNull();
    expect(orderGets(calls)).toHaveLength(2);
    await waitFor(() => expect(li).toHaveFocus());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows other errors in the alert banner without reloading", async () => {
    const calls = installOrdersFetch([[order()]], {
      status: 500,
      body: errorBody("INTERNAL_ERROR", "boom"),
    });
    renderPage();
    const button = await screen.findByRole("button", { name: CANCEL_NAME });
    fireEvent.click(button);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "We couldn't cancel this request. Try again.",
    );
    expect(orderGets(calls)).toHaveLength(1);
    expect(cancelButton()).not.toHaveAttribute("aria-disabled");
    expect(screen.queryByText(REQUEST_CHANGED_MESSAGE)).toBeNull();
  });
});
