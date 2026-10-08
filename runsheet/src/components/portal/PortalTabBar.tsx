"use client";

/**
 * Phone sections (< 768 px, D17): a bottom tab bar 64 px + the safe-area
 * inset, reachable one-handed. Each item is a 22 px icon over a 12 px label.
 */
import Link from "next/link";
import { isCurrentSection, type PortalNavItem } from "./portalNav";
import { focusRing } from "./styles";

export default function PortalTabBar({
  items,
  pathname,
}: {
  items: PortalNavItem[];
  pathname: string;
}) {
  return (
    <nav
      aria-label="Portal"
      data-portal-tabbar
      className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-surface pb-[env(safe-area-inset-bottom)]"
    >
      <ul
        className="mx-auto grid h-16 max-w-[640px]"
        style={{
          gridTemplateColumns: `repeat(${items.length}, minmax(0, 1fr))`,
        }}
      >
        {items.map((item) => {
          const current = isCurrentSection(pathname, item);
          const Icon = item.icon;
          return (
            <li key={item.href} className="flex">
              <Link
                href={item.href}
                aria-current={current ? "page" : undefined}
                className={`flex min-h-11 flex-1 flex-col items-center justify-center gap-0.5 text-xs font-semibold ${focusRing} ${
                  current ? "text-brand-700" : "text-slate-600"
                }`}
              >
                <span
                  aria-hidden="true"
                  className={`inline-flex h-7 w-11 items-center justify-center rounded-full ${current ? "bg-brand-50" : ""}`}
                >
                  <Icon
                    className="h-[22px] w-[22px]"
                    strokeWidth={current ? 2.4 : 2}
                  />
                </span>
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
