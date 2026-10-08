"use client";

/**
 * Sidebar (UI revamp R2.2, R2.6).
 *
 * Brand mark at the top (the top bar no longer carries it), the working set
 * first, a collapsible "Back office" group, and Settings pinned at the bottom.
 * Orders and Live carry count badges. Visibility goes through `canSee`; the
 * roles come from the shell, so nothing role-gated renders before they
 * resolve. The desktop rail can collapse to icons; below `md` the sidebar is
 * an off-canvas drawer.
 */
import { ChevronDown, ChevronLeft } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { hasAnyRole, visibleByCanSee } from "../../config/modules";
import {
  BACKOFFICE_STORAGE_KEY,
  NAV_SECTIONS,
  type NavItem,
  type NavSection,
} from "../../config/nav";

export interface NavCounts {
  orders?: number;
  live?: number;
}

export interface SidebarProps {
  activeItem?: string;
  /** `null` until the session's roles resolve. */
  roles: readonly string[] | null;
  isCollapsed: boolean;
  onToggle: () => void;
  onNavigate: (item: string) => void;
  counts?: NavCounts;
  isMobileOpen?: boolean;
  onMobileClose?: () => void;
}

function readBackoffice(): "open" | "closed" | null {
  try {
    const v = window.localStorage.getItem(BACKOFFICE_STORAGE_KEY);
    return v === "open" || v === "closed" ? v : null;
  } catch {
    return null;
  }
}

export default function Sidebar({
  activeItem = "today",
  roles,
  isCollapsed,
  onToggle,
  onNavigate,
  counts = {},
  isMobileOpen = false,
  onMobileClose,
}: SidebarProps) {
  const asideRef = useRef<HTMLElement>(null);
  const [isMobile, setIsMobile] = useState(false);
  const [stored, setStored] = useState<"open" | "closed" | null>(null);

  useEffect(() => setStored(readBackoffice()), []);

  const sections = useMemo(
    () =>
      NAV_SECTIONS.map((s) => ({
        ...s,
        items: visibleByCanSee(s.items, { roles }),
      })).filter((s) => s.items.length > 0),
    [roles],
  );

  // Collapsed by default unless the user is an admin; the user's own choice
  // wins once made. The group also opens while one of its pages is active.
  const isAdmin = hasAnyRole(roles, ["admin"]);
  const backofficeActive = NAV_SECTIONS.find(
    (s) => s.id === "backoffice",
  )?.items.some((i) => i.id === activeItem);
  const backofficeOpen =
    backofficeActive || (stored ? stored === "open" : isAdmin);

  const toggleBackoffice = () => {
    const next = backofficeOpen ? "closed" : "open";
    setStored(next);
    try {
      window.localStorage.setItem(BACKOFFICE_STORAGE_KEY, next);
    } catch {
      // Private mode: the choice lasts for this page view.
    }
  };

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(max-width: 767px)");
    const update = () => setIsMobile(mq.matches);
    update();
    mq.addEventListener?.("change", update);
    return () => mq.removeEventListener?.("change", update);
  }, []);

  useEffect(() => {
    if (!isMobileOpen) return;
    asideRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onMobileClose?.();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [isMobileOpen, onMobileClose]);

  const collapsed = isMobile ? false : isCollapsed;

  const navigateTo = (id: string) => {
    onNavigate(id);
    onMobileClose?.();
  };

  const renderItem = (item: NavItem) => {
    const isActive = activeItem === item.id;
    const Icon = item.icon;
    const count = item.count ? counts[item.count] : undefined;
    const showCount = typeof count === "number" && count > 0;
    return (
      <li key={item.id}>
        <button
          type="button"
          onClick={() => navigateTo(item.id)}
          aria-current={isActive ? "page" : undefined}
          aria-label={
            collapsed
              ? showCount
                ? `${item.label}, ${count}`
                : item.label
              : undefined
          }
          title={collapsed ? item.label : undefined}
          data-nav={item.id}
          className={`relative flex h-9 w-full items-center gap-2.5 rounded-lg text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-1 ${
            collapsed ? "justify-center px-0" : "px-2.5"
          } ${
            isActive
              ? "bg-primary text-on-primary"
              : "text-slate-800 hover:bg-slate-100"
          }`}
        >
          <Icon aria-hidden="true" className="h-4 w-4 shrink-0" />
          {!collapsed && <span className="truncate">{item.label}</span>}
          {showCount && !collapsed && (
            <span
              className={`ml-auto rounded-full px-1.5 text-[11px] font-bold tabular-nums ${
                isActive ? "bg-white/20 text-white" : "bg-red-100 text-red-800"
              }`}
            >
              {count}
              <span className="sr-only">
                {item.count === "orders"
                  ? " orders waiting"
                  : " need attention"}
              </span>
            </span>
          )}
          {showCount && collapsed && (
            <span
              aria-hidden="true"
              className="absolute right-1 top-1 h-2 w-2 rounded-full bg-red-600"
            />
          )}
        </button>
      </li>
    );
  };

  const renderSection = (section: NavSection & { items: NavItem[] }) => {
    if (section.collapsible) {
      const listId = `nav-${section.id}`;
      return (
        <div key={section.id} className="mt-3">
          {collapsed ? (
            <div aria-hidden="true" className="mx-2 mb-2 h-px bg-slate-200" />
          ) : (
            <button
              type="button"
              onClick={toggleBackoffice}
              aria-expanded={backofficeOpen}
              aria-controls={listId}
              className="flex h-7 w-full items-center gap-1 rounded-md px-2.5 text-[11px] font-semibold uppercase tracking-wider text-slate-600 hover:text-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              {section.label}
              <ChevronDown
                aria-hidden="true"
                className={`ml-auto h-3.5 w-3.5 transition-transform ${backofficeOpen ? "" : "-rotate-90"}`}
              />
            </button>
          )}
          {(backofficeOpen || collapsed) && (
            <ul id={listId} className="space-y-0.5">
              {section.items.map(renderItem)}
            </ul>
          )}
        </div>
      );
    }
    return (
      <ul key={section.id} className="space-y-0.5">
        {section.items.map(renderItem)}
      </ul>
    );
  };

  const main = sections.filter((s) => s.id !== "pinned");
  const pinned = sections.find((s) => s.id === "pinned");

  return (
    <>
      {isMobileOpen && (
        <button
          type="button"
          aria-label="Close navigation menu"
          onClick={onMobileClose}
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
        />
      )}
      <aside
        ref={asideRef}
        tabIndex={-1}
        aria-label="Sidebar navigation"
        className={`fixed inset-y-0 left-0 z-40 flex h-full w-56 shrink-0 flex-col border-r border-slate-200 bg-surface transition-all duration-200 focus:outline-none md:relative md:z-auto md:translate-x-0 ${
          isCollapsed ? "md:w-16" : "md:w-56"
        } ${isMobileOpen ? "translate-x-0" : "-translate-x-full"}`}
      >
        <div
          className={`flex h-12 shrink-0 items-center gap-2 border-b border-slate-200 ${collapsed ? "justify-center px-2" : "px-3"}`}
        >
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary">
            <img
              src="/runsheet_logo.svg"
              alt=""
              className="h-4 w-4"
              style={{ filter: "brightness(0) invert(1)" }}
            />
          </span>
          {!collapsed && (
            <span className="text-[15px] font-bold tracking-tight text-brand-700">
              Runsheet
            </span>
          )}
          <button
            onClick={onToggle}
            type="button"
            aria-label={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!isCollapsed}
            className={`hidden h-7 w-7 items-center justify-center rounded-md text-slate-600 hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus md:flex ${collapsed ? "absolute -right-3.5 top-2.5 border border-slate-200 bg-surface shadow-sm" : "ml-auto"}`}
          >
            <ChevronLeft
              aria-hidden="true"
              className={`h-4 w-4 transition-transform ${isCollapsed ? "rotate-180" : ""}`}
            />
          </button>
        </div>

        <nav
          aria-label="Primary"
          className="min-h-0 flex-1 overflow-y-auto px-2 py-2"
        >
          {main.map(renderSection)}
        </nav>

        {pinned && (
          <div className="shrink-0 border-t border-slate-200 px-2 py-2">
            <nav aria-label="Settings">{renderSection(pinned)}</nav>
          </div>
        )}
      </aside>
    </>
  );
}
