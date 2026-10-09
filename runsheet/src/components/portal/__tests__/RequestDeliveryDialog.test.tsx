/**
 * OID-4 and UI-3 for requesting a delivery (R4.6, R4.7, PD9, PD10), ported
 * from `OrderRequestForm.test.tsx` with the same assertions, plus the dialog
 * cases of R14.11 / R14.12 (preselect, focus return, sheet, unavailable).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";

jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: { attemptRefreshingSession: jest.fn() },
}));

import type { PortalTank } from "../../../services/portalApi";
import { errorBody, installFetch, tank } from "../__fixtures__/portal";
import { localIsoDate } from "../ids";
import RequestDeliveryDialog, {
  ORDERING_UNAVAILABLE_MESSAGE,
} from "../RequestDeliveryDialog";
import { tankTitles } from "../tankTitle";
import { buildWindow } from "../useOrderRequest";

const TANKS: PortalTank[] = [
  tank({ product_code: "DIESEL_2" }),
  tank({
    customer_tank_id: "QA-TANK-2",
    label: "South yard",
    capacity_gallons: 500,
    current_level_gallons: 100,
    product_code: "HEATING_OIL",
  }),
];

function Dialog({
  tanks = TANKS,
  orderingAvailable = true,
  initialTankId = null,
  onClose = () => {},
  onRetryTanks,
}: {
  tanks?: PortalTank[];
  orderingAvailable?: boolean;
  initialTankId?: string | null;
  onClose?: () => void;
  onRetryTanks?: () => void;
}) {
  return (
    <RequestDeliveryDialog
      open
      onClose={onClose}
      tanks={tanks}
      titles={tankTitles(tanks)}
      orderingAvailable={orderingAvailable}
      supplierName="QA Supplier"
      initialTankId={initialTankId}
      onRetryTanks={onRetryTanks}
    />
  );
}

function fillValidForm() {
  fireEvent.click(screen.getByLabelText("South yard"));
  fireEvent.click(screen.getByLabelText("Gallons"));
  fireEvent.change(screen.getByLabelText("Gallons requested"), {
    target: { value: "300" },
  });
  fireEvent.change(screen.getByLabelText("Delivery date"), {
    target: { value: localIsoDate(3) },
  });
  fireEvent.change(screen.getByLabelText("From"), {
    target: { value: "08:00" },
  });
  fireEvent.change(screen.getByLabelText("To"), {
    target: { value: "12:00" },
  });
  fireEvent.change(screen.getByLabelText("PO number (optional)"), {
    target: { value: "PO-77" },
  });
  fireEvent.change(screen.getByLabelText("Notes (optional)"), {
    target: { value: "Gate code at the office" },
  });
}

function submitButton() {
  return screen.getByRole("button", { name: /send request/i });
}

const CREATED = {
  status: 201,
  body: {
    data: {
      order_id: "ord_portal_1",
      status_code: "awaiting_confirmation",
      status_label: "Awaiting confirmation",
    },
    request_id: "r",
  },
};

const realMatchMedia = window.matchMedia;
afterEach(() => {
  jest.useRealTimers();
  window.matchMedia = realMatchMedia;
});

describe("OID-4", () => {
  it("shows_message_keeps_values_no_retry", async () => {
    const fetchMock = installFetch({
      status: 409,
      body: errorBody("ORDER_INTAKE_DISABLED", "Order intake is disabled"),
    });
    render(<Dialog />);
    fillValidForm();
    fireEvent.click(submitButton());
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        ORDERING_UNAVAILABLE_MESSAGE,
      ),
    );
    expect(submitButton()).toHaveAttribute("aria-disabled", "true");
    // Every value is kept.
    expect(screen.getByLabelText("South yard")).toBeChecked();
    expect(screen.getByLabelText("Gallons")).toBeChecked();
    expect(screen.getByLabelText("Gallons requested")).toHaveValue("300");
    expect(screen.getByLabelText("Delivery date")).toHaveValue(localIsoDate(3));
    expect(screen.getByLabelText("From")).toHaveValue("08:00");
    expect(screen.getByLabelText("To")).toHaveValue("12:00");
    expect(screen.getByLabelText("PO number (optional)")).toHaveValue("PO-77");
    expect(screen.getByLabelText("Notes (optional)")).toHaveValue(
      "Gate code at the office",
    );
    // aria-disabled doesn't block activation; the handler must.
    fireEvent.click(submitButton());
    fireEvent.submit(
      document.getElementById(
        submitButton().getAttribute("form") as string,
      ) as HTMLFormElement,
    );
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("UI-3 request dialog", () => {
  it("sends the request body the API expects and confirms in the dialog", async () => {
    const fetchMock = installFetch(CREATED);
    render(<Dialog />);
    fillValidForm();
    fireEvent.click(submitButton());
    expect(
      await screen.findByRole("heading", { name: "Request sent" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Awaiting confirmation")).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/portal\/orders$/);
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body).toMatchObject({
      customer_tank_id: "QA-TANK-2",
      quantity: { mode: "gallons", gallons: 300 },
      po_number: "PO-77",
      notes: "Gate code at the office",
      ...buildWindow(localIsoDate(3), "08:00", "12:00"),
    });
    expect(body.client_event_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
  });

  it("labels every control and bounds the date to today … today + 60 days", () => {
    render(<Dialog />);
    expect(
      screen.getByRole("dialog", { name: "Request a delivery" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("radiogroup", { name: "Tank" }),
    ).toBeInTheDocument();
    for (const label of [
      "North yard",
      "South yard",
      "Fill to full",
      "Gallons",
      "Delivery date",
      "From",
      "To",
      "PO number (optional)",
      "Notes (optional)",
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
    const date = screen.getByLabelText("Delivery date");
    expect(date).toHaveAttribute("type", "date");
    expect(date).toHaveAttribute("min", localIsoDate(0));
    expect(date).toHaveAttribute("max", localIsoDate(60));
    expect(screen.getByLabelText("From")).toHaveAttribute("type", "time");
    // Products are caps and names, never codes (R14.3).
    expect(document.body.textContent).not.toMatch(/\b[A-Z0-9]+_[A-Z0-9_]+\b/);
    expect(
      screen.getAllByText(/Heating oil \(No\. 2\)/).length,
    ).toBeGreaterThan(0);
  });

  it("uses NumberField for gallons (text input, whole gallons)", () => {
    render(<Dialog />);
    fireEvent.click(screen.getByLabelText("Gallons"));
    const gallons = screen.getByLabelText("Gallons requested");
    expect(gallons).toHaveAttribute("type", "text");
    expect(gallons).toHaveAttribute("inputmode", "numeric");
  });

  it("summarizes errors in an alert, focuses it and links each field", async () => {
    const fetchMock = installFetch({ body: {} });
    render(<Dialog />);
    fireEvent.click(screen.getByLabelText("Gallons"));
    fireEvent.change(screen.getByLabelText("Gallons requested"), {
      target: { value: "5000" },
    });
    fireEvent.change(screen.getByLabelText("From"), {
      target: { value: "09:00" },
    });
    fireEvent.click(submitButton());
    const summary = await screen.findByRole("alert");
    expect(summary).toHaveTextContent("Please fix the following");
    expect(summary).toHaveTextContent("Choose a delivery date.");
    expect(summary).toHaveTextContent(/no more than 2,000 gallons/);
    expect(summary).toHaveTextContent("Enter both a start and an end time");
    await waitFor(() => expect(summary).toHaveFocus());
    const gallons = screen.getByLabelText("Gallons requested");
    expect(gallons).toHaveAttribute("aria-invalid", "true");
    const describedBy = (gallons.getAttribute("aria-describedby") ?? "").split(
      " ",
    );
    const messages = describedBy.map(
      (id) => document.getElementById(id)?.textContent ?? "",
    );
    expect(messages.join(" ")).toMatch(/no more than 2,000 gallons/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a date beyond the 60-day horizon", async () => {
    installFetch({ body: {} });
    render(<Dialog />);
    fireEvent.change(screen.getByLabelText("Delivery date"), {
      target: { value: localIsoDate(61) },
    });
    fireEvent.click(submitButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Choose a date from today to 60 days ahead.",
    );
  });

  it("while ordering is unavailable: PD10 banner first, fields disabled, aria-disabled submit, no fetch (R14.12)", async () => {
    const fetchMock = installFetch({ body: {} });
    render(<Dialog orderingAvailable={false} />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(ORDERING_UNAVAILABLE_MESSAGE);
    // The banner comes before the first field.
    const firstField = screen.getByLabelText("North yard");
    expect(
      status.compareDocumentPosition(firstField) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(submitButton()).toHaveAttribute("aria-disabled", "true");
    for (const label of [
      "North yard",
      "South yard",
      "Fill to full",
      "Gallons",
      "Delivery date",
      "From",
      "To",
      "PO number (optional)",
      "Notes (optional)",
    ]) {
      expect(screen.getByLabelText(label)).toBeDisabled();
    }
    fireEvent.click(submitButton());
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("announces a 429 with the Retry-After wait and keeps the form usable", async () => {
    installFetch({
      status: 429,
      body: errorBody("RATE_LIMITED", "Too many"),
      headers: { "Retry-After": "42" },
    });
    render(<Dialog />);
    fillValidForm();
    fireEvent.click(submitButton());
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Too many requests. Try again in 42 seconds.",
      ),
    );
    expect(submitButton()).not.toHaveAttribute("aria-disabled");
  });

  it("keeps the same client_event_id for a manual retry after an error", async () => {
    const fetchMock = installFetch(
      { status: 500, body: errorBody("INTERNAL_ERROR", "boom") },
      CREATED,
    );
    render(<Dialog />);
    fillValidForm();
    fireEvent.click(submitButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "We couldn't send your request",
    );
    fireEvent.click(submitButton());
    await screen.findByRole("heading", { name: "Request sent" });
    const ids = fetchMock.mock.calls.map(
      ([, init]) =>
        JSON.parse((init as RequestInit).body as string).client_event_id,
    );
    expect(ids).toHaveLength(2);
    expect(ids[0]).toBe(ids[1]);
  });

  it("says the tank is gone on a 404 and reloads the tank list", async () => {
    installFetch({ status: 404, body: errorBody("NOT_FOUND", "nope") });
    const onRetryTanks = jest.fn();
    render(<Dialog onRetryTanks={onRetryTanks} />);
    fillValidForm();
    fireEvent.click(submitButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That tank isn't available any more.",
    );
    expect(onRetryTanks).toHaveBeenCalled();
  });

  it("sends a date-only request as local midnight to the next midnight", () => {
    const w = buildWindow("2026-11-01", "", "");
    expect(new Date(w.window_start).getTime()).toBe(
      new Date(2026, 10, 1).getTime(),
    );
    expect(new Date(w.window_end).getTime()).toBe(
      new Date(2026, 10, 2).getTime(),
    );
  });
});

describe("R14.11 dialog behaviour", () => {
  it("preselects the tank it was opened for", () => {
    render(<Dialog initialTankId="QA-TANK-2" />);
    expect(screen.getByLabelText("South yard")).toBeChecked();
    expect(screen.getByLabelText("North yard")).not.toBeChecked();
  });

  it("closes on Escape and returns focus to the trigger", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Request delivery for South yard
          </button>
          <RequestDeliveryDialog
            open={open}
            onClose={() => setOpen(false)}
            tanks={TANKS}
            titles={tankTitles(TANKS)}
            orderingAvailable
            supplierName="QA Supplier"
            initialTankId="QA-TANK-2"
          />
        </>
      );
    }
    render(<Harness />);
    const trigger = screen.getByRole("button", {
      name: "Request delivery for South yard",
    });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(screen.getByLabelText("South yard")).toHaveFocus(),
    );
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(trigger).toHaveFocus();
  });

  it("asks before discarding a changed request", async () => {
    const onClose = jest.fn();
    render(<Dialog onClose={onClose} />);
    fireEvent.change(screen.getByLabelText("PO number (optional)"), {
      target: { value: "PO-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      await screen.findByRole("alertdialog", { name: "Discard this request?" }),
    ).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("renders as a full-screen sheet below 640 px", () => {
    window.matchMedia = ((query: string) => ({
      matches: query.includes("max-width: 639.98px"),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    })) as unknown as typeof window.matchMedia;
    render(<Dialog />);
    expect(document.querySelector("[data-sheet]")).not.toBeNull();
    const panel = screen.getByRole("dialog");
    expect(panel.className).toMatch(/max-sm:h-\[100dvh\]/);
    expect(panel.className).toMatch(/max-sm:rounded-none/);
  });

  it("shows the empty state with the supplier when there are no tanks", () => {
    render(<Dialog tanks={[]} />);
    expect(screen.getByText("No tanks are set up yet.")).toBeInTheDocument();
    expect(screen.getByText(/Contact QA Supplier/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /send request/i })).toBeNull();
  });
});
