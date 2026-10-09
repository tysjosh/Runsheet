"use client";

/**
 * Tooltip: shown on hover and keyboard focus, dismissed with Escape, and wired
 * to the trigger with `aria-describedby` (WCAG 1.4.13: dismissible, hoverable,
 * persistent while hovered).
 */
import {
  cloneElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
  useEffect,
  useId,
  useState,
} from "react";

export interface TooltipProps {
  content: ReactNode;
  /** A single focusable element (button, link, input). */
  children: ReactElement<Record<string, unknown>>;
  side?: "top" | "bottom";
  /**
   * Link the bubble with aria-describedby (default). Turn off when the content
   * repeats the trigger's accessible name, as IconButton's does.
   */
  describe?: boolean;
  className?: string;
}

export function Tooltip({
  content,
  children,
  side = "bottom",
  describe = true,
  className = "",
}: TooltipProps) {
  const id = useId();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  if (!content) return children;
  const existing = (children.props["aria-describedby"] as string) || "";
  const trigger =
    describe && isValidElement(children)
      ? cloneElement(children, {
          "aria-describedby": open
            ? `${existing} ${id}`.trim()
            : existing || undefined,
        })
      : children;

  return (
    <span
      className={`relative inline-flex ${className}`}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      {trigger}
      {open && (
        <span
          role="tooltip"
          id={id}
          aria-hidden={describe ? undefined : true}
          className={`pointer-events-none absolute left-1/2 z-[70] w-max max-w-xs -translate-x-1/2 rounded-md bg-slate-900 px-2 py-1 text-xs font-normal leading-snug text-white shadow-lg ${
            side === "top" ? "bottom-full mb-1.5" : "top-full mt-1.5"
          }`}
        >
          {content}
        </span>
      )}
    </span>
  );
}
