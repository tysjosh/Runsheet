import {
  BarChart3,
  CalendarClock,
  ChevronLeft,
  ClipboardCheck,
  DollarSign,
  Fuel,
  LayoutDashboard,
  LogOut,
  Radio,
  Settings,
  Shield,
  SlidersHorizontal,
  Truck,
  User,
  Users,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import Session from "supertokens-auth-react/recipe/session";
import { apiService } from "../services/api";

interface SidebarProps {
  activeItem?: string;
  isCollapsed: boolean;
  onToggle: () => void;
  onNavigate: (item: string) => void;
  /**
   * Whether the off-canvas drawer is open. Only relevant below `md`; on
   * larger screens the sidebar is a permanent rail and this is ignored.
   */
  isMobileOpen?: boolean;
  /** Close the off-canvas drawer (backdrop click, Escape, or navigation). */
  onMobileClose?: () => void;
}

interface NavItem {
  id: string;
  label: string;
  icon: typeof LayoutDashboard;
}

// Grouped navigation — sections keep the 13 destinations scannable and put the
// three config-flavored destinations (Setup / Admin / Settings) together under
// one "Workspace" heading so they're no longer guessed-at siblings of ops.
const NAV_SECTIONS: { label: string; items: NavItem[] }[] = [
  {
    label: "Operations",
    items: [
      { id: "today", label: "Today", icon: LayoutDashboard },
      { id: "fleet", label: "Fleet", icon: Truck },
      { id: "dispatch", label: "Dispatch", icon: CalendarClock },
      { id: "drivers", label: "Drivers", icon: User },
      { id: "fuel-ops", label: "Fuel Ops", icon: Fuel },
      { id: "compliance", label: "Compliance", icon: ClipboardCheck },
      { id: "control", label: "Control Center", icon: Radio },
    ],
  },
  {
    label: "Commerce",
    items: [
      { id: "customers", label: "Customers", icon: Users },
      { id: "billing", label: "Billing", icon: DollarSign },
      { id: "analytics", label: "Analytics", icon: BarChart3 },
    ],
  },
  {
    label: "Workspace",
    items: [
      { id: "setup", label: "Setup", icon: SlidersHorizontal },
      { id: "admin", label: "Admin", icon: Shield },
      { id: "settings", label: "Settings", icon: Settings },
    ],
  },
];

export default function Sidebar({
  activeItem = "today",
  isCollapsed,
  onToggle,
  onNavigate,
  isMobileOpen = false,
  onMobileClose,
}: SidebarProps) {
  const router = useRouter();
  const [email, setEmail] = useState<string>("");
  const [isMobile, setIsMobile] = useState(false);
  const asideRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const mq = window.matchMedia("(max-width: 767px)");
    const update = () => setIsMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
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

  // On mobile the drawer is always full-width/expanded; collapse only applies
  // to the desktop rail.
  const collapsed = isMobile ? false : isCollapsed;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const profile = await apiService.getAccountProfile();
        if (!cancelled && profile?.email) setEmail(profile.email);
      } catch {
        // Non-fatal — the avatar just shows the generic label.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleLogout = async () => {
    try {
      await Session.signOut();
    } finally {
      router.push("/signin");
    }
  };

  // Navigating also dismisses the mobile drawer so the content is visible.
  const navigateTo = (id: string) => {
    onNavigate(id);
    onMobileClose?.();
  };

  const renderItem = (item: NavItem) => {
    const isActive = activeItem.toLowerCase() === item.id;
    const Icon = item.icon;
    return (
      <li key={item.id}>
        <button
          type="button"
          onClick={() => navigateTo(item.id)}
          aria-current={isActive ? "page" : undefined}
          title={collapsed ? item.label : undefined}
          className={`flex w-full items-center ${collapsed ? "justify-center rounded-2xl" : "justify-start gap-3 rounded-lg"} px-3 py-2.5 transition-colors duration-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-[color:var(--color-primary)] ${isActive ? "" : "hover:bg-[color-mix(in_srgb,var(--color-primary)_7%,transparent)]"}`}
          style={{
            color: isActive ? "white" : "var(--color-primary)",
            backgroundColor: isActive ? "var(--color-primary)" : "transparent",
          }}
        >
          <Icon
            className="h-5 w-5 flex-shrink-0"
            style={{ color: isActive ? "white" : "var(--color-primary)" }}
          />
          {!collapsed && (
            <span className="text-sm font-medium">{item.label}</span>
          )}
        </button>
      </li>
    );
  };

  return (
    <>
      {/* Backdrop — only rendered for the off-canvas drawer below `md`. */}
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
        className={`h-full transition-all duration-300 ease-in-out flex-shrink-0 fixed inset-y-0 left-0 z-40 md:relative md:z-auto md:translate-x-0 focus:outline-none w-60 ${isCollapsed ? "md:w-[72px]" : "md:w-60"} ${isMobileOpen ? "translate-x-0" : "-translate-x-full"}`}
        style={{
          backgroundColor: "var(--color-surface-muted)",
          borderRight:
            "1px solid color-mix(in srgb, var(--color-primary) 8%, transparent)",
        }}
      >
        {/* Toggle Button — desktop rail only */}
        <button
          onClick={onToggle}
          type="button"
          aria-label={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!isCollapsed}
          className="hidden md:block absolute -right-3 border rounded-full p-1.5 z-20 transition-all duration-200 shadow-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-[color:var(--color-primary)]"
          style={{
            backgroundColor: "var(--color-surface)",
            borderColor:
              "color-mix(in srgb, var(--color-primary) 12%, transparent)",
            top: "20px",
          }}
        >
          <ChevronLeft
            className={`w-4 h-4 transition-transform duration-300 ${isCollapsed ? "rotate-180" : ""}`}
            style={{ color: "var(--color-primary)" }}
          />
        </button>

        <nav
          aria-label="Primary"
          className="h-full overflow-y-auto p-4 pt-6 pb-28"
        >
          {NAV_SECTIONS.map((section, i) => (
            <div key={section.label} className={i > 0 ? "mt-6" : ""}>
              {collapsed ? (
                i > 0 && (
                  <div
                    className="mx-2 mb-3 h-px"
                    style={{
                      backgroundColor:
                        "color-mix(in srgb, var(--color-primary) 10%, transparent)",
                    }}
                  />
                )
              ) : (
                <p
                  className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-[0.18em]"
                  style={{ color: "var(--color-gray-500)" }}
                >
                  {section.label}
                </p>
              )}
              <ul className="space-y-1">{section.items.map(renderItem)}</ul>
            </div>
          ))}
        </nav>

        {/* User Profile - Expanded */}
        <div
          className={`absolute bottom-4 left-4 right-4 transition-all duration-300 ${collapsed ? "opacity-0 pointer-events-none" : "opacity-100"}`}
        >
          <div
            className="flex items-center space-x-3 p-3 rounded-lg"
            style={{
              backgroundColor:
                "color-mix(in srgb, var(--color-surface) 60%, transparent)",
            }}
          >
            <button
              type="button"
              onClick={() => navigateTo("profile")}
              title="View profile"
              className="flex items-center space-x-3 flex-1 min-w-0 text-left rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--color-primary)]"
            >
              <div className="w-9 h-9 rounded-full flex items-center justify-center flex-shrink-0 bg-primary">
                <User className="w-5 h-5 text-white" />
              </div>
              <div className="flex-1 min-w-0">
                <p
                  className="text-sm font-medium truncate"
                  style={{ color: "var(--color-primary)" }}
                >
                  {email || "My Account"}
                </p>
              </div>
            </button>
            <button
              type="button"
              onClick={handleLogout}
              className="flex-shrink-0 p-1.5 rounded-md transition-colors hover:bg-[color-mix(in_srgb,var(--color-error)_10%,transparent)] hover:text-[color:var(--color-error)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--color-error)]"
              style={{ color: "var(--color-gray-500)" }}
              title="Logout"
            >
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* User Profile - Collapsed */}
        <div
          className={`absolute bottom-4 left-1/2 transform -translate-x-1/2 transition-all duration-300 flex flex-col items-center gap-2 ${collapsed ? "opacity-100" : "opacity-0 pointer-events-none"}`}
        >
          <button
            type="button"
            onClick={() => navigateTo("profile")}
            className="w-10 h-10 rounded-full flex items-center justify-center transition-all duration-200 bg-primary hover:bg-primary-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-[color:var(--color-primary)]"
            title="View profile"
          >
            <User className="w-5 h-5 text-white" />
          </button>
          <button
            type="button"
            onClick={handleLogout}
            className="p-1.5 rounded-md transition-colors hover:text-[color:var(--color-error)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--color-error)]"
            style={{ color: "var(--color-gray-500)" }}
            title="Logout"
          >
            <LogOut className="w-4 h-4" />
          </button>
        </div>
      </aside>
    </>
  );
}
