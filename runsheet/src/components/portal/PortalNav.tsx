"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

interface NavItem {
  href: string;
  label: string;
  /** Overview matches only itself; sections match their sub-pages too. */
  exact?: boolean;
}

function isCurrent(pathname: string, item: NavItem): boolean {
  if (item.exact) return pathname === item.href;
  return pathname === item.href || pathname.startsWith(`${item.href}/`);
}

export default function PortalNav({
  invoicesAvailable,
}: {
  /** R5.6: the Invoices section is hidden while invoicing is off. */
  invoicesAvailable: boolean;
}) {
  const pathname = usePathname() ?? "/portal";
  const items: NavItem[] = [
    { href: "/portal", label: "Overview", exact: true },
    { href: "/portal/orders", label: "Orders" },
    ...(invoicesAvailable
      ? [{ href: "/portal/invoices", label: "Invoices" }]
      : []),
    { href: "/portal/tanks", label: "Tanks" },
  ];
  return (
    <nav aria-label="Portal" className="border-b border-gray-200 bg-white">
      <ul className="mx-auto flex max-w-5xl flex-wrap gap-1 px-4 py-2">
        {items.map((item) => {
          const current = isCurrent(pathname, item);
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={current ? "page" : undefined}
                className={`inline-flex min-h-10 min-w-6 items-center rounded-lg px-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary ${
                  current
                    ? "bg-primary-soft text-gray-900 underline underline-offset-4"
                    : "text-gray-700 hover:bg-gray-100"
                }`}
              >
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
