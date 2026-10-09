"use client";

/**
 * Drawer: right-hand side panel for read-only detail (R6.6). Same focus
 * behaviour as Modal (trap, Escape, focus return) via `useDialogA11y`.
 */
import { X } from "lucide-react";
import { type ReactNode, useId, useRef } from "react";
import { useDialogA11y } from "../../hooks/useDialogA11y";

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  width?: number;
  footer?: ReactNode;
}

export function Drawer({
  open,
  onClose,
  title,
  children,
  width = 480,
  footer,
}: DrawerProps) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useDialogA11y(open, ref, onClose);
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/20"
      onClick={onClose}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className="flex h-full max-w-full flex-col bg-surface shadow-xl focus:outline-none"
        style={{ width }}
      >
        <div className="flex h-12 shrink-0 items-center gap-2 border-b border-slate-200 px-4">
          <h2
            id={titleId}
            className="min-w-0 flex-1 truncate text-base font-semibold text-text"
          >
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-700 hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            <X aria-hidden="true" className="h-4 w-4" />
          </button>
        </div>
        <div
          // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must be keyboard-focusable (axe scrollable-region-focusable)
          tabIndex={0}
          className="min-h-0 flex-1 overflow-y-auto p-4 focus:outline-none"
        >
          {children}
        </div>
        {footer && (
          <div className="flex shrink-0 items-center justify-end gap-2 border-t border-slate-200 px-4 py-3">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
