import type { CustomerStatus } from "../../services/commerceApi";
import type { StatusKey } from "../../styles/tokens";

/** Customer status → badge style and label (icon + text, never colour alone). */
export const CUSTOMER_STATUS: Record<
  CustomerStatus,
  { status: StatusKey; label: string }
> = {
  active: { status: "ok", label: "Active" },
  archived: { status: "cancelled", label: "Archived" },
};
