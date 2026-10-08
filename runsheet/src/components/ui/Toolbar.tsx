"use client";

/**
 * Toolbar: the single 44 px control row under the title row (R4.3).
 *
 * Slots, left to right: `search`, `filters`, `views`, then `end`. It never
 * wraps. `overflow` actions render inline while they fit; when the row is too
 * narrow a ResizeObserver moves them, lowest priority first (highest
 * `priority` number), into the `⋯` menu. Items marked `inline: false` always
 * live in the menu.
 */
import { Ellipsis } from "lucide-react";
import {
  type ReactNode,
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Menu, type MenuItem } from "./Menu";

export interface ToolbarAction {
  id: string;
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  /** 1 = most important (collapses last). */
  priority?: number;
  inline?: boolean;
  disabled?: boolean;
}

export interface ToolbarProps {
  search?: ReactNode;
  filters?: ReactNode;
  views?: ReactNode;
  end?: ReactNode;
  overflow?: ToolbarAction[];
  label?: string;
  className?: string;
  /** Replaces the slots entirely (e.g. an active place mode, R8.3). */
  takeover?: ReactNode;
}

export function Toolbar({
  search,
  filters,
  views,
  end,
  overflow = [],
  label = "Toolbar",
  className = "",
  takeover,
}: ToolbarProps) {
  const rowRef = useRef<HTMLDivElement>(null);
  const [collapsed, setCollapsed] = useState(0);
  const [width, setWidth] = useState(0);

  // Inline candidates, most important first.
  const inline = overflow
    .filter((a) => a.inline !== false)
    .sort((a, b) => (a.priority ?? 5) - (b.priority ?? 5));
  const shownInline = inline.slice(0, Math.max(0, inline.length - collapsed));
  const inMenu = [
    ...inline.slice(shownInline.length),
    ...overflow.filter((a) => a.inline === false),
  ];

  const measure = useCallback(() => {
    const el = rowRef.current;
    if (!el) return;
    if (el.scrollWidth > el.clientWidth + 1 && collapsed < inline.length) {
      setCollapsed((c) => c + 1);
    }
  }, [collapsed, inline.length]);

  // Re-expand when the row grows, then collapse again until it fits.
  useLayoutEffect(() => {
    const el = rowRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[0]?.contentRect.width ?? 0);
      setWidth((prev) => {
        if (w > prev + 8) setCollapsed(0);
        return w;
      });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useLayoutEffect(() => {
    measure();
  }, [measure, width, overflow.length]);

  const menuItems: MenuItem[] = inMenu.map((a) => ({
    id: a.id,
    label: a.label,
    icon: a.icon,
    onSelect: a.onSelect,
    disabled: a.disabled,
  }));

  return (
    <div
      ref={rowRef}
      role="toolbar"
      aria-label={label}
      data-chrome="toolbar"
      className={`relative flex h-11 shrink-0 items-center gap-2 border-b border-slate-200 bg-surface px-4 ${className}`}
    >
      {takeover ?? (
        <>
          {search && (
            <div className="flex w-64 min-w-40 shrink items-center">
              {search}
            </div>
          )}
          {filters && (
            <div className="flex min-w-0 shrink items-center gap-2">
              {filters}
            </div>
          )}
          {views && (
            <div className="flex shrink-0 items-center gap-2">{views}</div>
          )}
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            {shownInline.map((a) => (
              <button
                key={a.id}
                type="button"
                onClick={a.onSelect}
                disabled={a.disabled}
                className="inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-lg px-2 text-xs font-medium text-slate-700 hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:opacity-50"
              >
                {a.icon && (
                  <span
                    aria-hidden="true"
                    className="inline-flex [&>svg]:h-3.5 [&>svg]:w-3.5"
                  >
                    {a.icon}
                  </span>
                )}
                {a.label}
              </button>
            ))}
            {end}
            {menuItems.length > 0 && (
              <Menu
                label="More actions"
                items={menuItems}
                trigger={(p) => (
                  <button
                    {...p}
                    type="button"
                    aria-label="More actions"
                    title="More actions"
                    className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-slate-700 hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                  >
                    <Ellipsis aria-hidden="true" className="h-4 w-4" />
                  </button>
                )}
              />
            )}
          </div>
        </>
      )}
    </div>
  );
}
