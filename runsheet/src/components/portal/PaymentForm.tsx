"use client";

/**
 * ACH payment for one invoice (R6, design §6.4 and §10.2).
 *
 * 1. Create posts with a UUID `Idempotency-Key` kept in `sessionStorage` per
 *    invoice until a terminal result, so a reload replays the same key.
 * 2. Stripe.js loads from https://js.stripe.com/v3 and the Payment Element
 *    mounts with the client secret, under a visible "Bank account" heading.
 * 3. `confirmPayment` runs with `redirect: "if_required"`.
 * 4. The attempt is polled every 3 s for at most 60 s, without the client
 *    secret, then the status label is shown. ACH stays "Payment processing"
 *    for days; that is expected.
 * 5. On reload with a stored attempt and no secret in memory, one call with
 *    `include_client_secret=true` decides whether the Element mounts again.
 *
 * No bank details are ever read or shown here; Stripe's iframe owns them.
 */

import Script from "next/script";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../services/api";
import {
  createPortalPayment,
  getPortalPaymentAttempt,
  hasErrorCode,
  isRateLimited,
  type PortalInvoice,
  rateLimitMessage,
  TERMINAL_PAYMENT_STATUSES,
} from "../../services/portalApi";
import type {
  StripeElements,
  StripeInstance,
  StripePaymentElement,
} from "../../types/stripe-js";
import { formatMoney } from "./format";
import { newUuid } from "./ids";
import LiveRegion from "./LiveRegion";
import { PAYMENTS_UNAVAILABLE_MESSAGE } from "./messages";
import {
  fieldError,
  fieldHint,
  fieldInput,
  fieldLabel,
  primaryButton,
} from "./styles";

export { PAYMENTS_UNAVAILABLE_MESSAGE };

export const STRIPE_JS_URL = "https://js.stripe.com/v3";
export const POLL_INTERVAL_MS = 3000;
export const POLL_LIMIT_MS = 60_000;
const MIN_CENTS = 100;

/** R6.13 labels, for responses that carry only a status code. */
export function paymentStatusLabel(code: string): string {
  if (code === "succeeded") return "Paid";
  if (code === "failed" || code === "canceled") {
    return "Payment failed, try again";
  }
  return "Payment processing";
}

interface StoredPayment {
  key: string;
  attemptId?: string;
}

export function paymentStorageKey(invoiceId: string): string {
  return `runsheet:portal-payment:${invoiceId}`;
}

function readStored(invoiceId: string): StoredPayment | null {
  try {
    const raw = window.sessionStorage.getItem(paymentStorageKey(invoiceId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredPayment;
    return typeof parsed?.key === "string" ? parsed : null;
  } catch {
    return null;
  }
}

function writeStored(invoiceId: string, value: StoredPayment): void {
  try {
    window.sessionStorage.setItem(
      paymentStorageKey(invoiceId),
      JSON.stringify(value),
    );
  } catch {
    // Storage can be unavailable (private mode); the key then lives in memory.
  }
}

function clearStored(invoiceId: string): void {
  try {
    window.sessionStorage.removeItem(paymentStorageKey(invoiceId));
  } catch {
    // Nothing to clear.
  }
}

/** Dollars text → cents, or null when it isn't a plain amount. */
export function parseDollars(text: string): number | null {
  const trimmed = text.trim().replace(/^\$/, "").replace(/,/g, "");
  if (!/^\d+(\.\d{0,2})?$/.test(trimmed)) return null;
  const [whole, frac = ""] = trimmed.split(".");
  return Number(whole) * 100 + Number(frac.padEnd(2, "0"));
}

function centsToInput(cents: number): string {
  return (cents / 100).toFixed(2);
}

type Phase =
  | "amount"
  | "starting"
  | "waiting"
  | "element"
  | "confirming"
  | "polling"
  | "done";

export default function PaymentForm({
  invoice,
  paymentsAvailable,
  onSettled,
}: {
  invoice: PortalInvoice;
  paymentsAvailable: boolean;
  /** Called once a terminal status lands, so the page can reload the invoice. */
  onSettled?: () => void;
}) {
  const uid = useId();
  const invoiceId = invoice.invoice_id;
  const remaining = invoice.remaining_cents;

  const [unavailable, setUnavailable] = useState(!paymentsAvailable);
  const [phase, setPhase] = useState<Phase>("amount");
  const [amount, setAmount] = useState(centsToInput(remaining));
  const [amountError, setAmountError] = useState<string | null>(null);
  const [alertText, setAlertText] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [secret, setSecret] = useState<{
    clientSecret: string;
    publishableKey: string;
  } | null>(null);
  const [attemptId, setAttemptId] = useState<string | null>(null);
  const [stripeReady, setStripeReady] = useState(
    () => typeof window !== "undefined" && Boolean(window.Stripe),
  );

  const containerRef = useRef<HTMLDivElement>(null);
  const stripeRef = useRef<StripeInstance | null>(null);
  const elementsRef = useRef<StripeElements | null>(null);
  const elementRef = useRef<StripePaymentElement | null>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      for (const t of timers.current) clearTimeout(t);
    };
  }, []);

  const later = useCallback((fn: () => void, ms: number) => {
    timers.current.push(setTimeout(fn, ms));
  }, []);

  const settle = useCallback(
    (code: string, label?: string) => {
      setStatus(label ?? paymentStatusLabel(code));
      if (TERMINAL_PAYMENT_STATUSES.has(code)) {
        clearStored(invoiceId);
        onSettled?.();
      }
    },
    [invoiceId, onSettled],
  );

  /** Poll without the client secret until terminal or the time limit. */
  const pollStatus = useCallback(
    (id: string, startedAt: number) => {
      later(async () => {
        if (!alive.current) return;
        try {
          const { data } = await getPortalPaymentAttempt(id);
          if (!alive.current) return;
          setStatus(data.status_label);
          if (TERMINAL_PAYMENT_STATUSES.has(data.status_code)) {
            settle(data.status_code, data.status_label);
            setPhase("done");
            return;
          }
        } catch (error) {
          if (isRateLimited(error)) setStatus(rateLimitMessage(error));
        }
        if (Date.now() - startedAt + POLL_INTERVAL_MS > POLL_LIMIT_MS) {
          setPhase("done");
          return;
        }
        pollStatus(id, startedAt);
      }, POLL_INTERVAL_MS);
    },
    [later, settle],
  );

  /** Ask for the client secret once; mount only when one comes back. */
  const fetchSecretOnce = useCallback(
    async (id: string) => {
      try {
        const { data } = await getPortalPaymentAttempt(id, {
          includeClientSecret: true,
        });
        if (!alive.current) return;
        if (data.client_secret && data.publishable_key) {
          setSecret({
            clientSecret: data.client_secret,
            publishableKey: data.publishable_key,
          });
          setPhase("element");
          return;
        }
        settle(data.status_code, data.status_label);
        setPhase(
          TERMINAL_PAYMENT_STATUSES.has(data.status_code) ? "amount" : "done",
        );
      } catch (error) {
        if (!alive.current) return;
        if (error instanceof ApiError && error.status === 404) {
          clearStored(invoiceId);
          setPhase("amount");
          return;
        }
        setAlertText(
          isRateLimited(error)
            ? rateLimitMessage(error)
            : "We couldn't check this payment. Please reload the page.",
        );
        setPhase("done");
      }
    },
    [invoiceId, settle],
  );

  /** A `creating` replay: poll until `created`, then fetch the secret once. */
  const waitForCreated = useCallback(
    (id: string, waitSeconds: number, startedAt: number) => {
      later(async () => {
        if (!alive.current) return;
        try {
          const { data } = await getPortalPaymentAttempt(id);
          if (!alive.current) return;
          if (data.status_code === "created") {
            await fetchSecretOnce(id);
            return;
          }
          if (data.status_code !== "creating") {
            settle(data.status_code, data.status_label);
            setPhase(
              TERMINAL_PAYMENT_STATUSES.has(data.status_code)
                ? "amount"
                : "done",
            );
            return;
          }
        } catch {
          // Keep waiting within the limit.
        }
        if (Date.now() - startedAt > POLL_LIMIT_MS) {
          setStatus(paymentStatusLabel("creating"));
          setPhase("done");
          return;
        }
        waitForCreated(id, waitSeconds, startedAt);
      }, Math.max(1, waitSeconds) * 1000);
    },
    [later, fetchSecretOnce, settle],
  );

  // Reload: a stored attempt and no secret in memory → one secret read.
  const reloadChecked = useRef(false);
  useEffect(() => {
    if (!paymentsAvailable || reloadChecked.current) return;
    reloadChecked.current = true;
    const stored = readStored(invoiceId);
    if (stored?.attemptId) {
      setAttemptId(stored.attemptId);
      setPhase("starting");
      void fetchSecretOnce(stored.attemptId);
    }
    // Runs once per invoice: the reload path reads the secret exactly once.
  }, [invoiceId, paymentsAvailable]);

  // Mount the Payment Element once Stripe.js and a secret are both ready.
  useEffect(() => {
    if (!secret || !stripeReady || !containerRef.current) return;
    const StripeCtor = window.Stripe;
    if (!StripeCtor) return;
    const stripe = StripeCtor(secret.publishableKey);
    const elements = stripe.elements({ clientSecret: secret.clientSecret });
    const element = elements.create("payment");
    element.mount(containerRef.current);
    stripeRef.current = stripe;
    elementsRef.current = elements;
    elementRef.current = element;
    return () => {
      element.destroy?.();
      elementRef.current = null;
    };
  }, [secret, stripeReady]);

  async function handleStart(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (phase !== "amount") return;
    setAlertText(null);
    const cents = parseDollars(amount);
    if (cents === null || cents < MIN_CENTS || cents > remaining) {
      setAmountError(
        `Enter an amount from ${formatMoney(MIN_CENTS)} to ${formatMoney(remaining)}.`,
      );
      return;
    }
    setAmountError(null);

    const stored = readStored(invoiceId);
    const key = stored?.key ?? newUuid();
    writeStored(invoiceId, { key, attemptId: stored?.attemptId });
    setPhase("starting");
    try {
      const result = await createPortalPayment(invoiceId, key, cents);
      if (!alive.current) return;
      const { data } = result;
      setAttemptId(data.payment_attempt_id);
      writeStored(invoiceId, { key, attemptId: data.payment_attempt_id });
      if (data.client_secret && data.publishable_key) {
        setSecret({
          clientSecret: data.client_secret,
          publishableKey: data.publishable_key,
        });
        setPhase("element");
      } else if (data.status_code === "creating") {
        setStatus(paymentStatusLabel("creating"));
        setPhase("waiting");
        waitForCreated(
          data.payment_attempt_id,
          result.retryAfterSeconds ?? 2,
          Date.now(),
        );
      } else {
        settle(data.status_code);
        setPhase(
          TERMINAL_PAYMENT_STATUSES.has(data.status_code) ? "amount" : "done",
        );
      }
    } catch (error) {
      if (!alive.current) return;
      setPhase("amount");
      if (hasErrorCode(error, "PORTAL_PAYMENTS_UNAVAILABLE")) {
        clearStored(invoiceId);
        setUnavailable(true);
      } else if (isRateLimited(error)) {
        setAlertText(rateLimitMessage(error));
      } else if (hasErrorCode(error, "PAYMENT_IN_PROGRESS")) {
        setAlertText(
          "A payment for this invoice is already in progress. Check back later.",
        );
      } else if (hasErrorCode(error, "INVOICE_NOT_PAYABLE")) {
        clearStored(invoiceId);
        setAlertText("This invoice can't be paid online right now.");
        onSettled?.();
      } else if (hasErrorCode(error, "PAYMENT_AMOUNT_INVALID")) {
        const max = (error as ApiError).details?.max_cents;
        setAmountError(
          `Enter an amount from ${formatMoney(MIN_CENTS)} to ${formatMoney(
            typeof max === "number" ? max : remaining,
          )}.`,
        );
      } else if (hasErrorCode(error, "IDEMPOTENCY_CONFLICT")) {
        // The stored key belongs to a different amount: start over cleanly.
        writeStored(invoiceId, { key: newUuid() });
        setAlertText(
          "The amount changed since this payment was started. Select Continue to start again.",
        );
      } else {
        // A provider error ends that attempt; the next try uses a new key.
        clearStored(invoiceId);
        setAlertText("The payment didn't start. Please try again.");
      }
    }
  }

  async function handleConfirm() {
    const stripe = stripeRef.current;
    const elements = elementsRef.current;
    if (phase !== "element" || !stripe || !elements || !attemptId) return;
    setAlertText(null);
    setPhase("confirming");
    const returnUrl = `${window.location.origin}/portal/invoices/${encodeURIComponent(
      invoiceId,
    )}?attempt=${encodeURIComponent(attemptId)}`;
    try {
      const result = await stripe.confirmPayment({
        elements,
        redirect: "if_required",
        confirmParams: { return_url: returnUrl },
      });
      if (!alive.current) return;
      if (result.error) {
        setAlertText(
          result.error.message ?? "The payment couldn't be confirmed.",
        );
        setPhase("element");
        return;
      }
      setStatus(paymentStatusLabel("pending"));
      setPhase("polling");
      pollStatus(attemptId, Date.now());
    } catch {
      if (!alive.current) return;
      setAlertText("The payment couldn't be confirmed. Please try again.");
      setPhase("element");
    }
  }

  if (unavailable) {
    return (
      <p className="text-sm text-gray-800">{PAYMENTS_UNAVAILABLE_MESSAGE}</p>
    );
  }

  const amountId = `${uid}-amount`;
  const bankHeadingId = `${uid}-bank`;
  const showElement =
    secret !== null && (phase === "element" || phase === "confirming");

  return (
    <div className="space-y-4">
      <Script
        src={STRIPE_JS_URL}
        strategy="afterInteractive"
        onReady={() => setStripeReady(true)}
      />
      <LiveRegion message={status} />
      {alertText && (
        <p
          role="alert"
          className="rounded-lg border border-error-light bg-error-light px-4 py-3 text-sm text-error-dark"
        >
          {alertText}
        </p>
      )}

      {(phase === "amount" || phase === "starting") && invoice.payable && (
        <form noValidate onSubmit={handleStart} className="space-y-3">
          <div>
            <label htmlFor={amountId} className={fieldLabel}>
              Payment amount (USD)
            </label>
            <input
              id={amountId}
              type="text"
              inputMode="decimal"
              autoComplete="off"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              aria-invalid={amountError ? true : undefined}
              aria-describedby={`${amountId}-hint${amountError ? ` ${amountId}-error` : ""}`}
              className={fieldInput}
            />
            <p id={`${amountId}-hint`} className={fieldHint}>
              From {formatMoney(MIN_CENTS)} up to the balance due,{" "}
              {formatMoney(remaining)}.
            </p>
            {amountError && (
              <p id={`${amountId}-error`} className={fieldError}>
                {amountError}
              </p>
            )}
          </div>
          <button
            type="submit"
            aria-disabled={phase === "starting" ? true : undefined}
            className={primaryButton}
          >
            {phase === "starting" ? "Starting payment…" : "Continue"}
          </button>
        </form>
      )}

      {(phase === "amount" || phase === "starting") && !invoice.payable && (
        <p className="text-sm text-gray-800">
          This invoice can't be paid online.
        </p>
      )}

      <section
        aria-labelledby={bankHeadingId}
        hidden={!showElement}
        className="space-y-3"
      >
        <h2 id={bankHeadingId} className="text-lg font-semibold text-gray-900">
          Bank account
        </h2>
        <div ref={containerRef} data-testid="payment-element" />
        {showElement && !stripeReady && (
          <p className="text-sm text-gray-700">Loading the payment form…</p>
        )}
        <button
          type="button"
          onClick={handleConfirm}
          aria-disabled={
            phase === "confirming" || !stripeReady ? true : undefined
          }
          className={primaryButton}
        >
          {phase === "confirming" ? "Paying…" : "Pay"}
        </button>
      </section>
    </div>
  );
}
