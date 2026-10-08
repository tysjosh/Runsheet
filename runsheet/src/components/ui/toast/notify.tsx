"use client";

/**
 * App-wide toasts. `notify({ type, message })` can be called from anything
 * (FormDialog calls it after a save, after it has already unmounted its
 * form), and the single `<GlobalToaster />` mounted by the dashboard shell
 * renders them in a polite live region. Same look and 4 s auto-dismiss as the
 * page-local `useToasts`.
 */
import { AlertTriangle, Check, X } from "lucide-react";
import { useEffect, useState } from "react";

export interface GlobalToast {
  id: number;
  /** `warning` (additive, Phase 3): a non-blocking risk, e.g. low parts. */
  type: "success" | "error" | "warning";
  message: string;
}

type Listener = (toasts: GlobalToast[]) => void;

let toasts: GlobalToast[] = [];
let nextId = 0;
const listeners = new Set<Listener>();
const emit = () => {
  for (const l of listeners) l(toasts);
};

export function dismissToast(id: number): void {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

export function notify({
  type,
  message,
  durationMs = 4000,
}: {
  type: "success" | "error" | "warning";
  message: string;
  durationMs?: number;
}): number {
  const id = ++nextId;
  toasts = [...toasts, { id, type, message }];
  emit();
  if (durationMs > 0) setTimeout(() => dismissToast(id), durationMs);
  return id;
}

/** Test helper. */
export function resetToasts(): void {
  toasts = [];
  emit();
}

export function GlobalToaster() {
  const [items, setItems] = useState<GlobalToast[]>(toasts);
  useEffect(() => {
    listeners.add(setItems);
    setItems(toasts);
    return () => {
      listeners.delete(setItems);
    };
  }, []);
  return (
    <div
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed right-4 top-14 z-[100] space-y-2"
    >
      {items.map((t) => (
        <div
          key={t.id}
          className={`pointer-events-auto flex items-center gap-2 rounded-lg px-4 py-3 text-sm font-medium text-white shadow-lg ${
            t.type === "success"
              ? "bg-primary"
              : t.type === "warning"
                ? "bg-warning"
                : "bg-danger"
          }`}
        >
          {t.type === "success" ? (
            <Check aria-hidden="true" className="h-4 w-4" />
          ) : (
            <AlertTriangle aria-hidden="true" className="h-4 w-4" />
          )}
          <span>{t.message}</span>
          <button
            type="button"
            onClick={() => dismissToast(t.id)}
            className="ml-2 rounded p-0.5 hover:bg-white/20"
            aria-label="Dismiss notification"
          >
            <X aria-hidden="true" className="h-3 w-3" />
          </button>
        </div>
      ))}
    </div>
  );
}
