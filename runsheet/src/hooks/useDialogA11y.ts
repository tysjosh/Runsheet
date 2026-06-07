import { type RefObject, useEffect } from "react";

/**
 * Accessibility behavior shared by modal dialogs and slide-over panels.
 *
 * While `isOpen`, this hook:
 *  • moves focus into the dialog on open (first focusable element, else the
 *    container itself),
 *  • traps Tab / Shift+Tab within the dialog so keyboard focus can't escape to
 *    the page behind it,
 *  • closes the dialog on Escape,
 *  • restores focus to the previously-focused element on close.
 *
 * The container element should be focusable as a fallback (e.g. `tabIndex={-1}`)
 * for the rare case where it has no focusable children.
 */
export function useDialogA11y(
  isOpen: boolean,
  containerRef: RefObject<HTMLElement | null>,
  onClose: () => void,
): void {
  useEffect(() => {
    if (!isOpen) return;

    const previouslyFocused = document.activeElement as HTMLElement | null;
    const container = containerRef.current;

    const focusableSelector =
      'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

    const getFocusable = (): HTMLElement[] =>
      container
        ? Array.from(container.querySelectorAll<HTMLElement>(focusableSelector))
        : [];

    // Initial focus: first focusable child, falling back to the container.
    const initial = getFocusable();
    if (initial.length > 0) {
      initial[0].focus();
    } else {
      container?.focus();
    }

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !container) return;

      const items = getFocusable();
      if (items.length === 0) {
        e.preventDefault();
        container.focus();
        return;
      }

      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;

      if (e.shiftKey) {
        if (active === first || !container.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !container.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown, true);
      // Restore focus to whatever was focused before the dialog opened.
      previouslyFocused?.focus?.();
    };
  }, [isOpen, containerRef, onClose]);
}
