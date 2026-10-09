"use client";

/**
 * Menu: a button that opens a `role="menu"` list (WAI-ARIA menu button).
 * Arrow keys move, Home/End jump, Enter/Space select, Escape closes and
 * returns focus to the trigger. Items can be grouped radio choices
 * (`role="menuitemradio"`) for view menus.
 */
import {
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

export interface MenuItem {
  id: string;
  label: ReactNode;
  icon?: ReactNode;
  onSelect?: () => void;
  disabled?: boolean;
  danger?: boolean;
  /** Radio items in a group: rendered as menuitemradio with aria-checked. */
  checked?: boolean;
  /** Group heading rendered before this item. */
  group?: string;
  /** Plain href (rendered as a link-like item). */
  href?: string;
}

export interface MenuProps {
  /** Render prop for the trigger; spread `props` onto a <button>. */
  trigger: (props: {
    ref: React.Ref<HTMLButtonElement>;
    "aria-haspopup": "menu";
    "aria-expanded": boolean;
    "aria-controls": string;
    onClick: () => void;
    onKeyDown: (e: React.KeyboardEvent) => void;
  }) => ReactNode;
  items: MenuItem[];
  align?: "start" | "end";
  label?: string;
  className?: string;
  /** `touch`: 44 px items for phone-sized targets (R14.19). Additive (3.11). */
  size?: "default" | "touch";
}

export function Menu({
  trigger,
  items,
  align = "end",
  label,
  className = "",
  size = "default",
}: MenuProps) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const menuId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const enabled = items
    .map((it, i) => (it.disabled ? -1 : i))
    .filter((i) => i >= 0);

  const close = useCallback((refocus = true) => {
    setOpen(false);
    if (refocus) triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (listRef.current?.contains(t) || triggerRef.current?.contains(t))
        return;
      close(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open, close]);

  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(
      `[data-index="${active}"]`,
    );
    el?.focus();
  }, [open, active]);

  const openAt = (which: "first" | "last") => {
    if (enabled.length === 0) return;
    setActive(which === "first" ? enabled[0] : enabled[enabled.length - 1]);
    setOpen(true);
  };

  const move = (delta: number) => {
    const pos = enabled.indexOf(active);
    const next = enabled[(pos + delta + enabled.length) % enabled.length];
    if (next !== undefined) setActive(next);
  };

  const select = (item: MenuItem) => {
    if (item.disabled) return;
    close(true);
    item.onSelect?.();
    if (item.href) window.location.assign(item.href);
  };

  const onListKey = (e: React.KeyboardEvent) => {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        move(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        move(-1);
        break;
      case "Home":
        e.preventDefault();
        setActive(enabled[0]);
        break;
      case "End":
        e.preventDefault();
        setActive(enabled[enabled.length - 1]);
        break;
      case "Escape":
        e.preventDefault();
        e.stopPropagation();
        close(true);
        break;
      case "Tab":
        close(false);
        break;
      default:
    }
  };

  const renderItem = (item: MenuItem, i: number) => {
    const isRadio = item.checked !== undefined;
    return (
      <button
        key={item.id}
        type="button"
        role={isRadio ? "menuitemradio" : "menuitem"}
        aria-checked={isRadio ? item.checked : undefined}
        aria-disabled={item.disabled || undefined}
        data-index={i}
        tabIndex={i === active ? 0 : -1}
        onClick={() => select(item)}
        onMouseEnter={() => !item.disabled && setActive(i)}
        className={`flex ${size === "touch" ? "h-11" : "h-8"} w-full items-center gap-2 px-3 text-left focus:bg-slate-100 focus:outline-none ${
          item.disabled
            ? "cursor-not-allowed text-slate-500"
            : item.danger
              ? "text-red-700 hover:bg-red-50"
              : "text-slate-800 hover:bg-slate-100"
        }`}
      >
        {isRadio && (
          <span
            aria-hidden="true"
            className={`h-2 w-2 rounded-full ${item.checked ? "bg-primary" : "border border-slate-400"}`}
          />
        )}
        {item.icon && (
          <span aria-hidden="true" className="inline-flex">
            {item.icon}
          </span>
        )}
        <span className="truncate">{item.label}</span>
      </button>
    );
  };

  return (
    <div className={`relative inline-flex ${className}`}>
      {trigger({
        ref: triggerRef,
        "aria-haspopup": "menu",
        "aria-expanded": open,
        "aria-controls": menuId,
        onClick: () => (open ? close(false) : openAt("first")),
        onKeyDown: (e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            openAt("first");
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            openAt("last");
          }
        },
      })}
      {open && (
        <div
          ref={listRef}
          id={menuId}
          role="menu"
          aria-label={label}
          tabIndex={-1}
          onKeyDown={onListKey}
          className={`absolute top-full z-[60] mt-1 min-w-[12rem] rounded-lg border border-slate-200 bg-surface py-1 text-sm shadow-lg ${
            align === "end" ? "right-0" : "left-0"
          }`}
        >
          {segments(items).map((seg) =>
            seg.group ? (
              <div key={`g-${seg.start}`} role="group" aria-label={seg.group}>
                <div
                  aria-hidden="true"
                  className="px-3 pb-1 pt-2 text-xs font-semibold text-text-muted"
                >
                  {seg.group}
                </div>
                {seg.items.map(({ item, index }) => renderItem(item, index))}
              </div>
            ) : (
              seg.items.map(({ item, index }) => renderItem(item, index))
            ),
          )}
        </div>
      )}
    </div>
  );
}

/** Splits items into consecutive segments that start at each `group` heading. */
function segments(items: MenuItem[]) {
  const out: {
    group?: string;
    start: number;
    items: { item: MenuItem; index: number }[];
  }[] = [];
  items.forEach((item, index) => {
    const last = out[out.length - 1];
    if (item.group || !last) {
      out.push({ group: item.group, start: index, items: [{ item, index }] });
    } else {
      last.items.push({ item, index });
    }
  });
  return out;
}
