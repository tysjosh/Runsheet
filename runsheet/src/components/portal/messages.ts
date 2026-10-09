/** Customer-facing portal texts that the spec fixes word for word. */

/** PD10 / R4.6. */
export const ORDERING_UNAVAILABLE_MESSAGE =
  "Online ordering is unavailable right now. Please contact your supplier to place this order.";

/** PD15. */
export const PAYMENTS_UNAVAILABLE_MESSAGE =
  "Online payment isn't available. Contact your supplier to pay.";

/** R5.6: shown when an invoice page is opened while invoicing is off. */
export const INVOICES_UNAVAILABLE_MESSAGE =
  "Invoices aren't available online right now.";

/** Shown after a cancel answers 409 ORDER_NOT_CANCELLABLE and the list reloads (same text as staff Confirm/Decline). */
export const REQUEST_CHANGED_MESSAGE =
  "This request changed. Reloaded the latest version.";

/** Announced after a customer cancels their own request (R4.10). */
export function requestCancelledMessage(reference: string): string {
  return `Request for ${reference} cancelled.`;
}

/** The customer's reference for an order: "{tank title}, {date}" (D29). */
export function orderReference(tankTitle: string, dateText: string): string {
  return dateText && dateText !== "—" ? `${tankTitle}, ${dateText}` : tankTitle;
}
