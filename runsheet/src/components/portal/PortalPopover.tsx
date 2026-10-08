"use client";

/**
 * A small non-modal disclosure: a button and a panel under it. Escape or an
 * outside click closes it and focus returns to the button. Below 640 px the
 * panel can render as a bottom sheet (`sheet`), as the invoice Dates control
 * does (R14.10).
 */
import {
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

export default function PortalPopover({
  label,
  buttonContent,
  buttonClassName,
  children,
  sheet = false,
  align = "end",
  panelLabel,
}: {
  /** Accessible name of the button (when its content is an icon). */
  label?: string;
  buttonContent: ReactNode;
  buttonClassName: string;
  children: (close: () => void) => ReactNode;
  sheet?: boolean;
  align?: "start" | "end";
  panelLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const close = useCallback(() => {
    setOpen(false);
    buttonRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t) || buttonRef.current?.contains(t))
        return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    // Start inside the panel.
    panelRef.current
      ?.querySelector<HTMLElement>("input, button, a[href]")
      ?.focus();
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);

  return (
    <div className="relative shrink-0">
      <button
        ref={buttonRef}
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className={buttonClassName}
      >
        {buttonContent}
      </button>
      {open && (
        <div
          ref={panelRef}
          id={panelId}
          role="group"
          aria-label={panelLabel}
          className={`z-50 rounded-xl border border-border bg-surface p-3 shadow-lg ${
            sheet
              ? "max-sm:fixed max-sm:inset-x-0 max-sm:bottom-0 max-sm:rounded-b-none max-sm:pb-[calc(12px+env(safe-area-inset-bottom))] sm:absolute sm:top-full sm:mt-1 sm:w-80"
              : "absolute top-full mt-1 w-64"
          } ${align === "end" ? "sm:right-0" : "sm:left-0"} ${sheet ? "" : align === "end" ? "right-0" : "left-0"}`}
        >
          {children(close)}
        </div>
      )}
    </div>
  );
}
