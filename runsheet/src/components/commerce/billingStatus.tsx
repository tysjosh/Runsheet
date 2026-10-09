/**
 * Billing status vocabularies mapped onto the shared status tokens (task 3.4).
 * Every badge is `StatusBadge` (hue + icon + label), so colour is never the
 * only signal; labels are sentence case, never raw codes.
 */
import type {
  AccountStatus,
  CreditState,
  InvoiceStatus,
  PaymentStatus,
  QboPushState,
} from "../../services/commerceApi";
import type { StatusKey } from "../../styles/tokens";
import { StatusBadge } from "../ui/StatusBadge";

type Style = { status: StatusKey; label: string };

export const INVOICE_STATUS: Record<InvoiceStatus, Style> = {
  draft: { status: "draft", label: "Draft" },
  open: { status: "open", label: "Open" },
  partial: { status: "partial", label: "Partially paid" },
  paid: { status: "paid", label: "Paid" },
  overdue: { status: "overdue", label: "Overdue" },
  void: { status: "cancelled", label: "Void" },
};

export const QBO_STATE: Record<QboPushState, Style> = {
  pending: { status: "draft", label: "Pending" },
  pushed: { status: "ok", label: "Synced" },
  retry: { status: "warning", label: "Retrying" },
  dead_letter: { status: "critical", label: "Failed" },
};

export const PAYMENT_STATUS: Record<PaymentStatus, Style> = {
  applied: { status: "paid", label: "Applied" },
  reversed: { status: "cancelled", label: "Reversed" },
};

export const ACCOUNT_STATUS: Record<AccountStatus, Style> = {
  active: { status: "ok", label: "Active" },
  suspended: { status: "warning", label: "Suspended" },
  closed: { status: "cancelled", label: "Closed" },
};

export const CREDIT_STATE: Record<CreditState, Style> = {
  ok: { status: "ok", label: "OK" },
  hold: { status: "critical", label: "On hold" },
  override: { status: "warning", label: "Override" },
};

function badge(map: Record<string, Style>, code: string | null | undefined) {
  const style = code ? map[code] : undefined;
  if (!style)
    return (
      <StatusBadge
        status="draft"
        label={code ? code.replace(/_/g, " ") : "Unknown"}
      />
    );
  return <StatusBadge status={style.status} label={style.label} />;
}

export const InvoiceStatusBadge = ({ status }: { status: string }) =>
  badge(INVOICE_STATUS, status);
export const QboStateBadge = ({ state }: { state: string }) =>
  badge(QBO_STATE, state);
export const PaymentStatusBadge = ({ status }: { status: string }) =>
  badge(PAYMENT_STATUS, status);
export const AccountStatusBadge = ({ status }: { status: string }) =>
  badge(ACCOUNT_STATUS, status);
export const CreditStateBadge = ({ state }: { state: string }) =>
  badge(CREDIT_STATE, state);
