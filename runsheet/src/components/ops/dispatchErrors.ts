/**
 * Toast text for a failed plan approve/dispatch (OI-16).
 *
 * Dispatch refuses plans that still have `placed` orders with
 * `ORDERS_NOT_CONFIRMED` and the ids in `details.order_ids`. The toast names
 * up to five of them so the dispatcher knows which orders to confirm.
 *
 * Duck-typed on `code`/`details` (like `classifyLoadError`) so it also works
 * when tests replace `services/api` with a partial mock.
 */

const MAX_IDS_SHOWN = 5;

export function approveErrorMessage(err: unknown): string {
  if (!(err instanceof Error)) return "Failed to approve plan";
  const { code, details } = err as Error & {
    code?: unknown;
    details?: { order_ids?: unknown };
  };
  if (code === "ORDERS_NOT_CONFIRMED") {
    const ids = Array.isArray(details?.order_ids)
      ? details.order_ids.filter((id): id is string => typeof id === "string")
      : [];
    if (ids.length > 0) {
      const shown = ids.slice(0, MAX_IDS_SHOWN).join(", ");
      const more =
        ids.length > MAX_IDS_SHOWN
          ? ` and ${ids.length - MAX_IDS_SHOWN} more`
          : "";
      return `Confirm these orders before dispatching: ${shown}${more}`;
    }
  }
  return err.message || "Failed to approve plan";
}
