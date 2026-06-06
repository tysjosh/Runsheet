/**
 * Shared Toast Notification System
 *
 * Single source of truth for the canonical success/error toast primitives that
 * were previously re-declared verbatim across multiple ops/admin pages
 * (FuelDistributionPage, DepotsPage, CustomerTankPage, TruckCompartmentsPage,
 * SourcingPage, RoadRestrictionsPanel, ReconciliationPage, the admin
 * integrations page, and the orders/[orderId] near-copy).
 *
 * Behavior is preserved exactly from those copies:
 *  • success/error variants
 *  • top-right placement
 *  • 4000ms auto-dismiss
 *  • manual dismiss
 *  • stacking
 *
 * A single module-scoped id counter replaces the per-file `toastIdCounter`s.
 *
 * NOTE: `components/ops/CreateJobModal.tsx` intentionally keeps its own,
 * divergent toast (warning/error variants, string ids, bottom-right placement,
 * aria-live) and is NOT migrated onto this module.
 */

import { AlertTriangle, Check, X } from "lucide-react";
import { useCallback, useState } from "react";

export interface Toast {
  id: number;
  message: string;
  type: "success" | "error";
}

let toastIdCounter = 0;

export function ToastContainer({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="fixed top-4 right-4 z-[100] space-y-2">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-sm font-medium ${
            toast.type === "success"
              ? "bg-success text-white"
              : "bg-error text-white"
          }`}
        >
          {toast.type === "success" ? (
            <Check className="w-4 h-4" />
          ) : (
            <AlertTriangle className="w-4 h-4" />
          )}
          <span>{toast.message}</span>
          <button
            type="button"
            onClick={() => onDismiss(toast.id)}
            className="ml-2 p-0.5 hover:bg-white/20 rounded"
            aria-label="Dismiss notification"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      ))}
    </div>
  );
}

export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((message: string, type: "success" | "error") => {
    const id = ++toastIdCounter;
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return { toasts, addToast, dismissToast };
}
