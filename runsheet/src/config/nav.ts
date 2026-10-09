/**
 * Sidebar navigation (UI revamp R2.2, audit §b "Proposed IA").
 *
 * Order puts the dispatcher's working set first: Dashboard, Dispatch, Orders,
 * Live, Fleet, Customers, Fuel. Billing, Compliance and Analytics sit in a
 * collapsible "Back office" group (collapsed by default for users without
 * `admin`; the choice is remembered in localStorage under `rs.nav.backoffice`).
 * Settings is pinned at the bottom.
 *
 * Every id must be registered in `config/modules.ts` (the drift guard in
 * `modules.test.ts` checks this array), because `canSee` fails closed on an
 * unknown id.
 */
import {
  BarChart3,
  CalendarClock,
  ClipboardCheck,
  DollarSign,
  Fuel,
  LayoutDashboard,
  type LucideIcon,
  Radio,
  Settings,
  ShoppingCart,
  Truck,
  Users,
} from "lucide-react";

export interface NavItem {
  id: string;
  label: string;
  icon: LucideIcon;
  /** Route; defaults to `dashboardPathForItem(id)`. */
  href?: string;
  /** Sidebar count badge source. */
  count?: "orders" | "live";
}

export interface NavSection {
  id: "main" | "backoffice" | "pinned";
  /** Visible heading; none for the main group. */
  label?: string;
  collapsible?: boolean;
  items: NavItem[];
}

export const BACKOFFICE_STORAGE_KEY = "rs.nav.backoffice";

export const NAV_SECTIONS: NavSection[] = [
  {
    id: "main",
    items: [
      { id: "today", label: "Dashboard", icon: LayoutDashboard },
      { id: "dispatch", label: "Dispatch", icon: CalendarClock },
      { id: "orders", label: "Orders", icon: ShoppingCart, count: "orders" },
      { id: "live", label: "Live", icon: Radio, count: "live" },
      { id: "fleet", label: "Fleet", icon: Truck },
      { id: "customers", label: "Customers", icon: Users },
      { id: "fuel-ops", label: "Fuel", icon: Fuel },
    ],
  },
  {
    id: "backoffice",
    label: "Back office",
    collapsible: true,
    items: [
      { id: "billing", label: "Billing", icon: DollarSign },
      { id: "compliance", label: "Compliance", icon: ClipboardCheck },
      { id: "analytics", label: "Analytics", icon: BarChart3 },
    ],
  },
  {
    id: "pinned",
    items: [{ id: "settings", label: "Settings", icon: Settings }],
  },
];

/**
 * The nav id that should look active for a `/dashboard/*` module segment.
 * `control` is labelled Live; retired module ids map to their new homes.
 */
export const ACTIVE_ALIASES: Record<string, string> = {
  control: "live",
  drivers: "fleet",
  notifications: "customers",
  setup: "settings",
  admin: "settings",
  profile: "",
};

export function navIdForSegment(segment: string): string {
  return ACTIVE_ALIASES[segment] ?? segment;
}
