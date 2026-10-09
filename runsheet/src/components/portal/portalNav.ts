import {
  ClipboardList,
  Fuel,
  House,
  type LucideIcon,
  ReceiptText,
} from "lucide-react";

export interface PortalNavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Home matches only itself; sections match their sub-pages too. */
  exact?: boolean;
}

/** The portal sections (R14.5). Invoices only while invoicing is on (R5.6). */
export function portalNavItems(invoicesAvailable: boolean): PortalNavItem[] {
  return [
    { href: "/portal", label: "Home", icon: House, exact: true },
    { href: "/portal/orders", label: "Orders", icon: ClipboardList },
    ...(invoicesAvailable
      ? [{ href: "/portal/invoices", label: "Invoices", icon: ReceiptText }]
      : []),
    { href: "/portal/tanks", label: "Tanks", icon: Fuel },
  ];
}

export function isCurrentSection(
  pathname: string,
  item: PortalNavItem,
): boolean {
  if (item.exact) return pathname === item.href;
  return pathname === item.href || pathname.startsWith(`${item.href}/`);
}
