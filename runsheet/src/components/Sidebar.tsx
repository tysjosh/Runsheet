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

export default function Sidebar({
  activeItem = "fleet",
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

  // Track viewport so the drawer always renders its expanded content below
  // `md` even if the user previously collapsed the desktop rail. The
  // expand/collapse affordance is desktop-only.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const mq = window.matchMedia("(max-width: 767px)");
    const update = () => setIsMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  // When the drawer opens on small screens, move focus into it and let Escape
  // close it for keyboard users.
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

  // Surface the signed-in user's email next to the avatar. Derived from the
  // verified session via the backend, falling back to a generic label.
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
    // Revoke the SuperTokens session (clears the session cookies) before
    // returning to sign-in. Navigate regardless of revoke outcome.
    try {
      await Session.signOut();
    } finally {
      router.push("/signin");
    }
  };

  const menuItems = [
    { id: "today", label: "Today", icon: LayoutDashboard },
    { id: "fleet", label: "Fleet", icon: Truck },
    { id: "customers", label: "Customers", icon: Users },
    { id: "drivers", label: "Drivers", icon: User },
    { id: "dispatch", label: "Dispatch", icon: CalendarClock },
    { id: "fuel-ops", label: "Fuel Ops", icon: Fuel },
    { id: "compliance", label: "Compliance", icon: ClipboardCheck },
    { id: "billing", label: "Billing", icon: DollarSign },
    { id: "analytics", label: "Analytics", icon: BarChart3 },
    { id: "control", label: "Control Center", icon: Radio },
    { id: "setup", label: "Setup", icon: SlidersHorizontal },
    { id: "admin", label: "Admin", icon: Shield },
    { id: "settings", label: "Settings", icon: Settings },
  ];

  // Navigating also dismisses the mobile drawer so the content is visible.
  const navigateTo = (id: string) => {
    onNavigate(id);
    onMobileClose?.();
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
          aria-label={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!isCollapsed}
          className="hidden md:block absolute -right-3 border rounded-full p-1.5 z-20 transition-all duration-200 shadow-sm focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-gray-500"
          style={{
            backgroundColor: "var(--color-surface)",
            borderColor:
              "color-mix(in srgb, var(--color-primary) 12%, transparent)",
            top: "20px",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.backgroundColor = "var(--color-primary)";
            e.currentTarget.style.borderColor = "var(--color-primary)";
            const icon = e.currentTarget.querySelector("svg");
            if (icon) icon.style.color = "white";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.backgroundColor = "var(--color-surface)";
            e.currentTarget.style.borderColor =
              "color-mix(in srgb, var(--color-primary) 12%, transparent)";
            const icon = e.currentTarget.querySelector("svg");
            if (icon) icon.style.color = "var(--color-primary)";
          }}
        >
          <ChevronLeft
            className={`w-4 h-4 transition-transform duration-300 ${isCollapsed ? "rotate-180" : ""}`}
            style={{ color: "var(--color-primary)" }}
          />
        </button>

        <nav className="p-4 pt-6">
          <ul className="space-y-1.5">
            {menuItems.map((item) => (
              <li key={item.id}>
                <div
                  className={`flex items-center ${collapsed ? "justify-center" : "justify-start"} px-3 py-2.5 ${collapsed ? "rounded-2xl" : "rounded-lg"} cursor-pointer transition-all duration-200`}
                  style={{
                    color:
                      activeItem.toLowerCase() === item.id
                        ? "white"
                        : "var(--color-primary)",
                    backgroundColor:
                      activeItem.toLowerCase() === item.id
                        ? "var(--color-primary)"
                        : "transparent",
                  }}
                  onMouseEnter={(e) => {
                    if (activeItem.toLowerCase() !== item.id) {
                      e.currentTarget.style.backgroundColor =
                        "color-mix(in srgb, var(--color-primary) 6%, transparent)";
                    } else {
                      e.currentTarget.style.backgroundColor =
                        "var(--color-primary-hover)";
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (activeItem.toLowerCase() !== item.id) {
                      e.currentTarget.style.backgroundColor = "transparent";
                    } else {
                      e.currentTarget.style.backgroundColor =
                        "var(--color-primary)";
                    }
                  }}
                  onClick={() => navigateTo(item.id)}
                  title={collapsed ? item.label : ""}
                >
                  <div
                    className={`flex items-center ${collapsed ? "" : "space-x-3"}`}
                  >
                    <item.icon
                      className={`w-5 h-5 transition-colors`}
                      style={{
                        color:
                          activeItem.toLowerCase() === item.id
                            ? "white"
                            : "var(--color-primary)",
                      }}
                    />
                    {!collapsed && (
                      <span
                        className="font-medium text-sm transition-opacity duration-200"
                        style={{ opacity: collapsed ? 0 : 1 }}
                      >
                        {item.label}
                      </span>
                    )}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </nav>

        {/* User Profile - Expanded */}
        <div
          className={`absolute bottom-4 left-4 right-4 transition-all duration-300 ${collapsed ? "opacity-0 pointer-events-none" : "opacity-100"}`}
        >
          <div
            className="flex items-center space-x-3 p-3 rounded-lg transition-colors"
            style={{
              backgroundColor:
                "color-mix(in srgb, var(--color-surface) 60%, transparent)",
            }}
          >
            <button
              type="button"
              onClick={() => navigateTo("profile")}
              title="View profile"
              className="flex items-center space-x-3 flex-1 min-w-0 text-left focus:outline-none focus:ring-2 focus:ring-primary rounded-md"
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
              onClick={handleLogout}
              className="flex-shrink-0 transition-all duration-200 p-1.5 rounded-md"
              style={{ color: "var(--color-gray-500)" }}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor =
                  "color-mix(in srgb, var(--color-error) 10%, transparent)";
                e.currentTarget.style.color = "var(--color-error)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor = "transparent";
                e.currentTarget.style.color = "var(--color-gray-500)";
              }}
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
            onClick={() => navigateTo("profile")}
            className="w-10 h-10 rounded-full flex items-center justify-center transition-all duration-200 bg-primary hover:bg-primary-hover"
            onMouseEnter={(e) => {
              e.currentTarget.style.backgroundColor =
                "var(--color-primary-hover)";
              e.currentTarget.style.transform = "scale(1.05)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.backgroundColor = "var(--color-primary)";
              e.currentTarget.style.transform = "scale(1)";
            }}
            title="View profile"
          >
            <User className="w-5 h-5 text-white" />
          </button>
          <button
            onClick={handleLogout}
            className="p-1.5 rounded-md transition-colors"
            style={{ color: "var(--color-gray-500)" }}
            onMouseEnter={(e) => {
              e.currentTarget.style.color = "var(--color-error)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.color = "var(--color-gray-500)";
            }}
            title="Logout"
          >
            <LogOut className="w-4 h-4" />
          </button>
        </div>
      </aside>
    </>
  );
}
