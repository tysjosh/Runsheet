import { type RefObject, useEffect } from "react";

/**
 * Focus inside one of these means an open popup owns Escape: a combobox or
 * menu button whose popup is expanded, or the listbox/menu itself.
 */
const OPEN_POPUP_SELECTOR = [
  '[role="combobox"][aria-expanded="true"]',
  '[aria-haspopup]:not([aria-haspopup="false"])[aria-expanded="true"]',
  '[role="listbox"]',
  '[role="menu"]',
].join(", ");

/**
 * Open dialogs, oldest first. Only the topmost one handles Escape and Tab, so
 * a dialog opened from another dialog (e.g. a rule editor over the price-book
 * dialog, task 3.4) doesn't close or trap its parent's keys. Additive: a
 * single open dialog behaves exactly as before.
 */
const OPEN_DIALOGS: symbol[] = [];

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
    const token = Symbol("dialog");
    OPEN_DIALOGS.push(token);

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
      if (OPEN_DIALOGS[OPEN_DIALOGS.length - 1] !== token) return;
      if (e.key === "Escape") {
        // An open popup inside the dialog (listbox, menu) closes first; the
        // dialog closes on the next Escape. Only popup owners count: an
        // expanded disclosure or accordion toggle (plain aria-expanded) must
        // not swallow Escape.
        const t = e.target as Element | null;
        if (t?.closest?.(OPEN_POPUP_SELECTOR)) {
          return;
        }
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
      const at = OPEN_DIALOGS.indexOf(token);
      if (at >= 0) OPEN_DIALOGS.splice(at, 1);
      // Restore focus to whatever was focused before the dialog opened.
      previouslyFocused?.focus?.();
    };
  }, [isOpen, containerRef, onClose]);
}
