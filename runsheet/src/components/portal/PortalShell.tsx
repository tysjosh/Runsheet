"use client";

/**
 * The portal's own chrome (D17, design §11.1): a skip link, one 56 px top bar
 * (supplier mark, supplier and customer names, the section tabs from 768 px,
 * the account menu), `main#main`, and below 768 px a 64 px bottom tab bar.
 *
 * It must import nothing from the staff shell: `components/shell/*`,
 * Sidebar, Header, AIChat, GlobalSearch, NotificationBell, any WebSocket hook,
 * or the dashboard shell context. `app/portal/layout.test.tsx` (T-UI-SHELL)
 * enforces this.
 */
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { signOut } from "../../utils/auth";
import PortalAccountMenu from "./PortalAccountMenu";
import PortalTabBar from "./PortalTabBar";
import { isCurrentSection, portalNavItems } from "./portalNav";
import { focusRing, space } from "./styles";
import { PORTAL_TABS_IN_TOP_BAR, useMediaQuery } from "./useMediaQuery";

export default function PortalShell({
  supplierName,
  customerName,
  email,
  invoicesAvailable,
  children,
}: {
  supplierName: string;
  customerName: string;
  email: string;
  invoicesAvailable: boolean;
  children: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname() ?? "/portal";
  const wide = useMediaQuery(PORTAL_TABS_IN_TOP_BAR);
  const [signingOut, setSigningOut] = useState(false);
  const items = portalNavItems(invoicesAvailable);

  const handleSignOut = async () => {
    if (signingOut) return;
    setSigningOut(true);
    try {
      await signOut();
    } finally {
      router.replace("/signin");
    }
  };

  const mark = (supplierName.trim()[0] ?? "R").toUpperCase();

  return (
    <div className="min-h-screen bg-surface text-[15px] text-text max-md:text-base md:bg-canvas">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-2 focus:z-50 focus:rounded-lg focus:bg-surface focus:px-4 focus:py-2 focus:text-sm focus:font-semibold focus:text-link focus:shadow"
      >
        Skip to content
      </a>
      <header
        data-portal-topbar
        className="sticky top-0 z-40 h-14 border-b border-border bg-surface"
      >
        <div
          className={`mx-auto flex h-14 max-w-[1120px] items-center gap-3 py-2.5 ${space.gutter}`}
        >
          <span
            aria-hidden="true"
            className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-brand-600 text-[13px] font-extrabold text-white"
          >
            {mark}
          </span>
          {/* portal-fixes A2: the names take the width they need and only
              truncate (with a title) when the bar genuinely runs out. */}
          <div className="min-w-0 flex-1 md:flex-initial">
            <p
              className="truncate text-xs leading-4 text-text-muted"
              title={supplierName}
            >
              {supplierName}
            </p>
            <p
              className="truncate text-[15px] font-bold leading-[18px] text-text"
              title={customerName}
            >
              {customerName}
            </p>
          </div>
          {wide && (
            <nav
              aria-label="Portal"
              className="ml-4 flex h-14 shrink-0 self-stretch"
            >
              <ul className="flex h-14 items-stretch gap-1">
                {items.map((item) => {
                  const current = isCurrentSection(pathname, item);
                  return (
                    <li key={item.href} className="flex">
                      <Link
                        href={item.href}
                        aria-current={current ? "page" : undefined}
                        className={`flex items-center border-b-[3px] px-3.5 text-sm font-semibold ${focusRing} ${
                          current
                            ? "border-brand-600 text-brand-800"
                            : "border-transparent text-slate-700 hover:text-text"
                        }`}
                      >
                        {item.label}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </nav>
          )}
          <PortalAccountMenu
            email={email}
            supplierName={supplierName}
            customerName={customerName}
            onSignOut={() => void handleSignOut()}
            signingOut={signingOut}
          />
        </div>
      </header>
      <main
        id="main"
        tabIndex={-1}
        className={`mx-auto max-w-[1120px] ${space.gutter} pb-[calc(88px+env(safe-area-inset-bottom))] focus:outline-none md:pb-10`}
      >
        {children}
      </main>
      {!wide && <PortalTabBar items={items} pathname={pathname} />}
    </div>
  );
}
