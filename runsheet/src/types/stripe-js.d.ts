/**
 * Minimal declarations for the Stripe.js v3 subset the portal pay page uses
 * (customer portal design §10.2). Stripe.js is loaded from
 * https://js.stripe.com/v3 at runtime, so no npm package is added; only the
 * calls below are typed.
 */

export interface StripePaymentElement {
  mount(target: string | HTMLElement): void;
  unmount?(): void;
  destroy?(): void;
}

export interface StripeElements {
  create(
    type: "payment",
    options?: Record<string, unknown>,
  ): StripePaymentElement;
}

export interface StripeConfirmPaymentResult {
  error?: { type?: string; code?: string; message?: string };
  paymentIntent?: { id: string; status: string };
}

export interface StripeInstance {
  elements(options: { clientSecret: string }): StripeElements;
  confirmPayment(options: {
    elements: StripeElements;
    redirect?: "if_required" | "always";
    confirmParams?: { return_url?: string };
  }): Promise<StripeConfirmPaymentResult>;
}

export type StripeConstructor = (publishableKey: string) => StripeInstance;

declare global {
  interface Window {
    Stripe?: StripeConstructor;
  }
}
