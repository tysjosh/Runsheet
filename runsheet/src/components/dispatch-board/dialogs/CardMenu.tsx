/**
 * Card and lane action menu (design K14.4, R18.1). A popup `role="menu"`
 * anchored to the card that opened it. Arrow keys / Home / End move between
 * items, Enter or click runs one, Escape or Tab closes and returns focus to
 * the card. Disabled items stay focusable (`aria-disabled`) and show their
 * reason as text, so a keyboard or screen reader user learns why.
 */
import { type KeyboardEvent, useEffect, useLayoutEffect, useRef } from "react";
import type { MenuRequest } from "../BoardContext";

export interface CardMenuProps {
  request: MenuRequest | null;
  onClose: () => void;
}

export function CardMenu({ request, onClose }: CardMenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  const anchor = request?.anchor ?? null;

  useLayoutEffect(() => {
    if (!request) return;
    const first = ref.current?.querySelector<HTMLElement>('[role="menuitem"]');
    first?.focus();
  }, [request]);

  useEffect(() => {
    if (!request) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [request, onClose]);

  if (!request) return null;

  const close = (refocus = true) => {
    onClose();
    if (refocus) anchor?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(
      ref.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [],
    );
    const i = items.indexOf(document.activeElement as HTMLElement);
    let next: HTMLElement | undefined;
    switch (e.key) {
      case "ArrowDown":
        next = items[(i + 1) % items.length];
        break;
      case "ArrowUp":
        next = items[(i - 1 + items.length) % items.length];
        break;
      case "Home":
        next = items[0];
        break;
      case "End":
        next = items[items.length - 1];
        break;
      case "Escape":
        e.preventDefault();
        e.stopPropagation();
        close();
        return;
      case "Tab":
        close(false);
        return;
      default:
        return;
    }
    e.preventDefault();
    next?.focus();
  };

  const rect = anchor?.getBoundingClientRect();
  const style = rect
    ? {
        top: Math.min(rect.bottom + 4, window.innerHeight - 16),
        left: Math.max(8, Math.min(rect.left, window.innerWidth - 260)),
      }
    : { top: 80, left: 80 };

  return (
    <div
      ref={ref}
      role="menu"
      aria-label={request.label}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      className="fixed z-50 min-w-56 max-w-xs rounded-md border border-gray-200 bg-white py-1 shadow-lg"
      style={style}
    >
      {request.items.map((item) => (
        <button
          key={item.label}
          type="button"
          role="menuitem"
          aria-disabled={item.disabled || undefined}
          tabIndex={-1}
          onClick={() => {
            if (item.disabled) return;
            onClose();
            item.onSelect();
          }}
          className={`flex min-h-9 w-full flex-col items-start px-3 py-1.5 text-left text-sm focus:bg-gray-100 focus:outline-none ${
            item.disabled
              ? "cursor-not-allowed text-gray-400"
              : "text-gray-800 hover:bg-gray-50"
          }`}
        >
          <span>{item.label}</span>
          {item.disabled && item.reason && (
            <span className="text-xs text-gray-500">{item.reason}</span>
          )}
        </button>
      ))}
    </div>
  );
}

export default CardMenu;
