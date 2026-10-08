/**
 * A status shown as text first (PD11, R6.13): the label always carries the
 * meaning, and the tint is decoration only, so nothing is conveyed by color.
 */

const TONES: Record<string, string> = {
  // Orders
  awaiting_confirmation: "bg-warning-light text-warning-dark",
  on_hold: "bg-warning-light text-warning-dark",
  confirmed: "bg-info-light text-info-dark",
  out_for_delivery: "bg-info-light text-info-dark",
  delivered: "bg-success-light text-success-dark",
  not_delivered: "bg-error-light text-error-dark",
  cancelled: "bg-gray-100 text-gray-800",
  // Invoices
  open: "bg-info-light text-info-dark",
  partial: "bg-warning-light text-warning-dark",
  paid: "bg-success-light text-success-dark",
  overdue: "bg-error-light text-error-dark",
  void: "bg-gray-100 text-gray-800",
  // Payment attempts
  creating: "bg-info-light text-info-dark",
  created: "bg-info-light text-info-dark",
  pending: "bg-info-light text-info-dark",
  succeeded: "bg-success-light text-success-dark",
  failed: "bg-error-light text-error-dark",
  canceled: "bg-error-light text-error-dark",
};

export default function StatusText({
  code,
  label,
}: {
  code: string;
  label: string;
}) {
  const tone = TONES[code] ?? "bg-gray-100 text-gray-800";
  return (
    <span
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-sm font-medium ${tone}`}
      data-status={code}
    >
      {label}
    </span>
  );
}
