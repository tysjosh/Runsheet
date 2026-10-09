/**
 * Order hold reasons the UI acts on.
 *
 * Kept out of `ordersApi.ts` so suites that mock the API client still see the
 * real value.
 */

/**
 * Hold reason the intake pipeline stamps on customer-portal requests until a
 * dispatcher confirms them (`fuel.order_models.PORTAL_REVIEW_HOLD_REASON`).
 */
export const PORTAL_REVIEW_HOLD_REASON = "awaiting_dispatcher_confirmation";
