/**
 * UI-3 for the ACH pay form (R6, PD15, design §6.4): unavailable text,
 * 429 text, Idempotency-Key persistence, Payment Element mount, confirm with
 * `redirect: "if_required"`, the 3 s / 60 s poll without the client secret,
 * and the single `include_client_secret=true` read on reload.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

const mockScripts: Array<Record<string, unknown>> = [];
jest.mock("next/script", () => ({
  __esModule: true,
  default: (props: Record<string, unknown>) => {
    mockScripts.push(props);
    return null;
  },
}));

jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: { attemptRefreshingSession: jest.fn() },
}));

import { errorBody, installFetch, invoice } from "../__fixtures__/portal";
import PaymentForm, {
  PAYMENTS_UNAVAILABLE_MESSAGE,
  POLL_INTERVAL_MS,
  paymentStorageKey,
  STRIPE_JS_URL,
} from "../PaymentForm";

const INV = invoice();
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function fakeStripe() {
  const element = { mount: jest.fn(), destroy: jest.fn() };
  const elements = { create: jest.fn(() => element) };
  const confirmPayment = jest.fn(async () => ({
    paymentIntent: { id: "pi_1", status: "processing" },
  }));
  const instance = { elements: jest.fn(() => elements), confirmPayment };
  const ctor = jest.fn(() => instance);
  window.Stripe = ctor as unknown as typeof window.Stripe;
  return { ctor, instance, elements, element, confirmPayment };
}

function created(overrides: Record<string, unknown> = {}) {
  return {
    status: 201,
    body: {
      data: {
        payment_attempt_id: "ppa_1",
        status_code: "created",
        amount_cents: INV.remaining_cents,
        client_secret: "pi_1_secret_x",
        publishable_key: "pk_test_x",
        ...overrides,
      },
      request_id: "r",
    },
  };
}

function attempt(status_code: string, status_label: string, extra = {}) {
  return {
    body: {
      data: {
        payment_attempt_id: "ppa_1",
        invoice_id: INV.invoice_id,
        status_code,
        status_label,
        amount_cents: INV.remaining_cents,
        created_at: "2026-10-08T12:00:00Z",
        client_secret: null,
        publishable_key: null,
        ...extra,
      },
      request_id: "r",
    },
  };
}

beforeEach(() => {
  mockScripts.length = 0;
  window.sessionStorage.clear();
  delete window.Stripe;
});

afterEach(() => {
  jest.useRealTimers();
});

describe("availability", () => {
  it("shows the PD15 text and no form or Stripe.js when payments are unavailable", () => {
    const fetchMock = installFetch({ body: {} });
    render(<PaymentForm invoice={INV} paymentsAvailable={false} />);
    expect(screen.getByText(PAYMENTS_UNAVAILABLE_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByLabelText(/payment amount/i)).toBeNull();
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
    expect(mockScripts).toHaveLength(0);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("switches to the PD15 text on 409 PORTAL_PAYMENTS_UNAVAILABLE", async () => {
    installFetch({
      status: 409,
      body: errorBody("PORTAL_PAYMENTS_UNAVAILABLE", "unavailable"),
    });
    render(<PaymentForm invoice={INV} paymentsAvailable />);
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(
      await screen.findByText(PAYMENTS_UNAVAILABLE_MESSAGE),
    ).toBeInTheDocument();
    expect(
      window.sessionStorage.getItem(paymentStorageKey(INV.invoice_id)),
    ).toBeNull();
  });

  it("loads Stripe.js from js.stripe.com after interactive", () => {
    installFetch({ body: {} });
    render(<PaymentForm invoice={INV} paymentsAvailable />);
    expect(mockScripts[0]).toMatchObject({
      src: STRIPE_JS_URL,
      strategy: "afterInteractive",
    });
  });
});

describe("starting a payment", () => {
  it("validates the amount from $1.00 to the balance", async () => {
    const fetchMock = installFetch({ body: {} });
    render(<PaymentForm invoice={INV} paymentsAvailable />);
    const amount = screen.getByLabelText("Payment amount (USD)");
    expect(amount).toHaveValue("1050.00");
    expect(amount).toHaveAttribute("inputmode", "decimal");
    fireEvent.change(amount, { target: { value: "0.50" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(
      await screen.findByText("Enter an amount from $1.00 to $1,050.00."),
    ).toBeInTheDocument();
    expect(amount).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(amount, { target: { value: "1050.01" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the 429 wait from Retry-After", async () => {
    installFetch({
      status: 429,
      body: errorBody("RATE_LIMITED", "Too many"),
      headers: { "Retry-After": "30" },
    });
    render(<PaymentForm invoice={INV} paymentsAvailable />);
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many requests. Try again in 30 seconds.",
    );
  });

  it("sends a stored Idempotency-Key, mounts the Payment Element, confirms and polls without the secret", async () => {
    jest.useFakeTimers();
    const stripe = fakeStripe();
    const onSettled = jest.fn();
    const fetchMock = installFetch(
      created(),
      attempt("pending", "Payment processing"),
      attempt("succeeded", "Paid"),
    );
    render(
      <PaymentForm invoice={INV} paymentsAvailable onSettled={onSettled} />,
    );
    fireEvent.change(screen.getByLabelText("Payment amount (USD)"), {
      target: { value: "500" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(stripe.element.mount).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/portal\/invoices\/QA-INV-1\/payments$/);
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toMatch(UUID_RE);
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      amount_cents: 50000,
    });
    const stored = JSON.parse(
      window.sessionStorage.getItem(paymentStorageKey(INV.invoice_id)) ?? "{}",
    );
    expect(stored).toEqual({
      key: headers["Idempotency-Key"],
      attemptId: "ppa_1",
    });
    expect(stripe.ctor).toHaveBeenCalledWith("pk_test_x");
    expect(stripe.instance.elements).toHaveBeenCalledWith({
      clientSecret: "pi_1_secret_x",
    });
    expect(stripe.elements.create).toHaveBeenCalledWith("payment");
    expect(
      screen.getByRole("heading", { name: "Bank account" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Pay" }));
    await waitFor(() => expect(stripe.confirmPayment).toHaveBeenCalledTimes(1));
    expect(stripe.confirmPayment).toHaveBeenCalledWith(
      expect.objectContaining({
        redirect: "if_required",
        confirmParams: {
          return_url: `${window.location.origin}/portal/invoices/QA-INV-1?attempt=ppa_1`,
        },
      }),
    );

    await act(async () => {
      await jest.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Payment processing");
    await act(async () => {
      await jest.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Paid");

    const pollUrls = fetchMock.mock.calls.slice(1).map(([u]) => String(u));
    expect(pollUrls).toHaveLength(2);
    for (const u of pollUrls) {
      expect(u).toMatch(/\/portal\/payment-attempts\/ppa_1$/);
      expect(u).not.toContain("include_client_secret");
    }
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(
      window.sessionStorage.getItem(paymentStorageKey(INV.invoice_id)),
    ).toBeNull();
  });

  it("stops polling after 60 seconds and keeps the processing label", async () => {
    jest.useFakeTimers();
    const stripe = fakeStripe();
    const fetchMock = installFetch(
      created(),
      attempt("pending", "Payment processing"),
    );
    render(<PaymentForm invoice={INV} paymentsAvailable />);
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(stripe.element.mount).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Pay" }));
    await waitFor(() => expect(stripe.confirmPayment).toHaveBeenCalled());

    await act(async () => {
      await jest.advanceTimersByTimeAsync(90_000);
    });
    const polls = fetchMock.mock.calls.length - 1;
    expect(polls).toBeGreaterThan(0);
    expect(polls).toBeLessThanOrEqual(60_000 / POLL_INTERVAL_MS);
    expect(screen.getByRole("status")).toHaveTextContent("Payment processing");
  });

  it("reuses the stored key on a retry after a non-terminal error", async () => {
    const fetchMock = installFetch(
      {
        status: 409,
        body: errorBody("PAYMENT_IN_PROGRESS", "in progress"),
      },
      { status: 409, body: errorBody("PAYMENT_IN_PROGRESS", "in progress") },
    );
    render(<PaymentForm invoice={INV} paymentsAvailable />);
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "already in progress",
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const keys = fetchMock.mock.calls.map(
      ([, init]) =>
        ((init as RequestInit).headers as Record<string, string>)[
          "Idempotency-Key"
        ],
    );
    expect(keys[0]).toBe(keys[1]);
  });
});

describe("reload", () => {
  it("reads the client secret exactly once and remounts the Element", async () => {
    const stripe = fakeStripe();
    window.sessionStorage.setItem(
      paymentStorageKey(INV.invoice_id),
      JSON.stringify({ key: "k-1234567890", attemptId: "ppa_1" }),
    );
    const fetchMock = installFetch(
      attempt("created", "Payment processing", {
        client_secret: "pi_1_secret_x",
        publishable_key: "pk_test_x",
      }),
    );
    render(<PaymentForm invoice={INV} paymentsAvailable />);
    await waitFor(() => expect(stripe.element.mount).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(
      /\/portal\/payment-attempts\/ppa_1\?include_client_secret=true$/,
    );
  });

  it("shows the status and does not mount when no secret comes back", async () => {
    const stripe = fakeStripe();
    window.sessionStorage.setItem(
      paymentStorageKey(INV.invoice_id),
      JSON.stringify({ key: "k-1234567890", attemptId: "ppa_1" }),
    );
    const fetchMock = installFetch(attempt("pending", "Payment processing"));
    render(<PaymentForm invoice={INV} paymentsAvailable />);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Payment processing",
      ),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(stripe.element.mount).not.toHaveBeenCalled();
  });
});
