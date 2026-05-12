import {
  BarChart3,
  CalendarClock,
  ChevronLeft,
  DollarSign,
  Fuel,
  ListChecks,
  LogOut,
  Settings,
  Shield,
  Truck,
  User,
  Users,
} from "lucide-react";
import { useRouter } from "next/navigation";

interface SidebarProps {
  activeItem?: string;
  isCollapsed: boolean;
  onToggle: () => void;
  onNavigate: (item: string) => void;
}

export default function Sidebar({
  activeItem = "fleet",
  isCollapsed,
  onToggle,
  onNavigate,
}: SidebarProps) {
  const router = useRouter();

  const handleLogout = () => {
    sessionStorage.removeItem("isAuthenticated");
    router.push("/signin");
  };

  const menuItems = [
    { id: "fleet", label: "Fleet", icon: Truck },
    { id: "customers", label: "Customers", icon: Users },
    { id: "drivers", label: "Drivers", icon: User },
    { id: "dispatch", label: "Dispatch", icon: CalendarClock },
    { id: "fuel-ops", label: "Fuel Ops", icon: Fuel },
    { id: "billing", label: "Billing", icon: DollarSign },
    { id: "reconciliation", label: "Reconciliation", icon: ListChecks },
    { id: "analytics", label: "Analytics", icon: BarChart3 },
    { id: "admin", label: "Admin", icon: Shield },
    { id: "settings", label: "Settings", icon: Settings },
  ];

  const handleItemClick = (id: string) => {
    onNavigate(id);
  };

  return (
    <aside
      className={`h-full transition-all duration-300 ease-in-out relative flex-shrink-0`}
      style={{
        backgroundColor: "var(--color-surface-muted)",
        width: isCollapsed ? "72px" : "240px",
        borderRight:
          "1px solid color-mix(in srgb, var(--color-primary) 8%, transparent)",
      }}
    >
      {/* Toggle Button */}
      <button
        onClick={onToggle}
        aria-label={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
        aria-expanded={!isCollapsed}
        className="absolute -right-3 border rounded-full p-1.5 z-20 transition-all duration-200 shadow-sm focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-gray-500"
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
                className={`flex items-center ${isCollapsed ? "justify-center" : "justify-start"} px-3 py-2.5 ${isCollapsed ? "rounded-2xl" : "rounded-lg"} cursor-pointer transition-all duration-200`}
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
                onClick={() => handleItemClick(item.id)}
                title={isCollapsed ? item.label : ""}
              >
                <div
                  className={`flex items-center ${isCollapsed ? "" : "space-x-3"}`}
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
                  {!isCollapsed && (
                    <span
                      className="font-medium text-sm transition-opacity duration-200"
                      style={{ opacity: isCollapsed ? 0 : 1 }}
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
        className={`absolute bottom-4 left-4 right-4 transition-all duration-300 ${isCollapsed ? "opacity-0 pointer-events-none" : "opacity-100"}`}
      >
        <div
          className="flex items-center space-x-3 p-3 rounded-lg transition-colors"
          style={{
            backgroundColor:
              "color-mix(in srgb, var(--color-surface) 60%, transparent)",
          }}
        >
          <div className="w-9 h-9 rounded-full flex items-center justify-center flex-shrink-0 bg-primary">
            <User className="w-5 h-5 text-white" />
          </div>
          <div className="flex-1 min-w-0">
            <p
              className="text-sm font-medium"
              style={{ color: "var(--color-primary)" }}
            >
              User
            </p>
          </div>
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
        className={`absolute bottom-4 left-1/2 transform -translate-x-1/2 transition-all duration-300 ${isCollapsed ? "opacity-100" : "opacity-0 pointer-events-none"}`}
      >
        <button
          onClick={handleLogout}
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
          title="Logout"
        >
          <User className="w-5 h-5 text-white" />
        </button>
      </div>
    </aside>
  );
}
