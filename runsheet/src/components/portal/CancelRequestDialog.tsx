"use client";

/**
 * Confirm step before a customer cancels a request (R4.10, PD8; owner
 * decision 2026-10-09). The shared Modal: "Keep request" has focus first;
 * Esc, the × and the backdrop keep the request, and focus goes back to the
 * Cancel button that opened it. The body names the tank (and product), the
 * window and the quantity in one sentence, never the order id (D29).
 */
import { useEffect, useId, useRef } from "react";
import type { PortalOrder } from "../../services/portalApi";
import { Modal } from "../ui/Modal";
import { window as formatWindow, productName, volume } from "./portalFormat";
import { dangerButton, secondaryButton } from "./styles";

export const CANCEL_DIALOG_TITLE = "Cancel this delivery request?";

/**
 * "This cancels your request for 1,600 gal of Diesel #2 for North yard,
 * delivery Mon 12 Oct, 8:00 AM – 12:00 PM CDT."
 */
export function cancelRequestSummary(
  order: PortalOrder,
  title: string,
  unit = "gal",
): string {
  const quantity = order.fill_to_full
    ? "a fill to full"
    : order.gallons_requested !== null
      ? volume(Math.round(order.gallons_requested), unit)
      : "a delivery";
  const product = order.product_code ? productName(order.product_code) : null;
  // A fallback title is already "{product} tank"; don't say it twice.
  const ofProduct =
    product && !title.toLowerCase().includes(product.toLowerCase())
      ? ` of ${product}`
      : "";
  const when = order.window_start
    ? `delivery ${formatWindow(order.window_start, order.window_end)}`
    : "with no delivery time set";
  return `This cancels your request for ${quantity}${ofProduct} for ${title}, ${when}.`;
}

export default function CancelRequestDialog({
  order,
  title,
  unit = "gal",
  onKeep,
  onConfirm,
}: {
  /** The request to cancel; `null` keeps the dialog closed. */
  order: PortalOrder | null;
  title: string;
  unit?: string;
  onKeep: () => void;
  onConfirm: (order: PortalOrder) => void;
}) {
  const bodyId = useId();
  const keepRef = useRef<HTMLButtonElement>(null);
  const open = order !== null;

  // Modal focuses the × first; start on the safe choice instead.
  useEffect(() => {
    if (open) keepRef.current?.focus();
  }, [open]);

  return (
    <Modal
      isOpen={open}
      onClose={onKeep}
      title={CANCEL_DIALOG_TITLE}
      size="sm"
      closeLabel="Close"
      describedById={bodyId}
      footer={
        <>
          <button
            ref={keepRef}
            type="button"
            className={`${secondaryButton} max-sm:flex-1`}
            onClick={onKeep}
          >
            Keep request
          </button>
          <button
            type="button"
            className={`${dangerButton} max-sm:flex-1`}
            onClick={() => order && onConfirm(order)}
          >
            Cancel request
          </button>
        </>
      }
    >
      <p id={bodyId} className="text-[15px] leading-6 text-text">
        {order ? cancelRequestSummary(order, title, unit) : null}
      </p>
    </Modal>
  );
}
