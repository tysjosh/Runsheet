/**
 * OID-4 and UI-3 for the delivery request form (R4.6, R4.7, PD9, PD10).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: { attemptRefreshingSession: jest.fn() },
}));

import { errorBody, installFetch, tank } from "../__fixtures__/portal";
import { localIsoDate } from "../ids";
import OrderRequestForm, {
  buildWindow,
  ORDERING_UNAVAILABLE_MESSAGE,
} from "../OrderRequestForm";

const TANKS = [
  tank(),
  tank({
    customer_tank_id: "QA-TANK-2",
    label: "South yard",
    capacity_gallons: 500,
  }),
];

function fillValidForm() {
  fireEvent.change(screen.getByLabelText("Tank"), {
    target: { value: "QA-TANK-2" },
  });
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

afterEach(() => {
  jest.useRealTimers();
});

describe("OID-4", () => {
  it("shows_message_keeps_values_no_retry", async () => {
    const fetchMock = installFetch({
      status: 409,
      body: errorBody("ORDER_INTAKE_DISABLED", "Order intake is disabled"),
    });
    render(<OrderRequestForm tanks={TANKS} orderingAvailable />);
    fillValidForm();

    fireEvent.click(submitButton());

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        ORDERING_UNAVAILABLE_MESSAGE,
      ),
    );
    expect(submitButton()).toHaveAttribute("aria-disabled", "true");

    // Every value is kept.
    expect(screen.getByLabelText("Tank")).toHaveValue("QA-TANK-2");
    expect(screen.getByLabelText("Gallons")).toBeChecked();
    expect(screen.getByLabelText("Gallons requested")).toHaveValue(300);
    expect(screen.getByLabelText("Delivery date")).toHaveValue(localIsoDate(3));
    expect(screen.getByLabelText("From")).toHaveValue("08:00");
    expect(screen.getByLabelText("To")).toHaveValue("12:00");
    expect(screen.getByLabelText("PO number (optional)")).toHaveValue("PO-77");
    expect(screen.getByLabelText("Notes (optional)")).toHaveValue(
      "Gate code at the office",
    );

    // aria-disabled doesn't block activation; the handler must.
    fireEvent.click(submitButton());
    fireEvent.submit(submitButton().closest("form") as HTMLFormElement);
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("UI-3 order form", () => {
  it("sends the request body the API expects", async () => {
    const fetchMock = installFetch({
      status: 201,
      body: {
        data: {
          order_id: "ord_portal_1",
          status_code: "awaiting_confirmation",
          status_label: "Awaiting confirmation",
        },
        request_id: "r",
      },
    });
    render(<OrderRequestForm tanks={TANKS} orderingAvailable />);
    fillValidForm();
    fireEvent.click(submitButton());

    expect(
      await screen.findByRole("heading", { name: "Request sent" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Awaiting confirmation/)).toBeInTheDocument();
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
    render(<OrderRequestForm tanks={TANKS} orderingAvailable />);
    for (const label of [
      "Tank",
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
  });

  it("summarizes errors in an alert, focuses it and links each field", async () => {
    const fetchMock = installFetch({ body: {} });
    render(<OrderRequestForm tanks={TANKS} orderingAvailable />);
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
    render(<OrderRequestForm tanks={TANKS} orderingAvailable />);
    fireEvent.change(screen.getByLabelText("Delivery date"), {
      target: { value: localIsoDate(61) },
    });
    fireEvent.click(submitButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Choose a date from today to 60 days ahead.",
    );
  });

  it("shows the PD10 message and disables submit while ordering is unavailable", async () => {
    const fetchMock = installFetch({ body: {} });
    render(<OrderRequestForm tanks={TANKS} orderingAvailable={false} />);
    expect(screen.getByRole("status")).toHaveTextContent(
      ORDERING_UNAVAILABLE_MESSAGE,
    );
    expect(submitButton()).toHaveAttribute("aria-disabled", "true");
    fillValidForm();
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
    render(<OrderRequestForm tanks={TANKS} orderingAvailable />);
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
      {
        status: 201,
        body: {
          data: {
            order_id: "o",
            status_code: "awaiting_confirmation",
            status_label: "Awaiting confirmation",
          },
          request_id: "r",
        },
      },
    );
    render(<OrderRequestForm tanks={TANKS} orderingAvailable />);
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
