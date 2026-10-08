"use client";

/**
 * The portal's own chrome (PD20, design §10.2): a skip link, a header with the
 * supplier and customer names and Sign out, the portal nav, and `main#main`.
 *
 * It must import nothing from the staff shell: Sidebar, Header, AIChat,
 * GlobalSearch, NotificationBell, any WebSocket hook, or the dashboard shell
 * context. `app/portal/layout.test.tsx` (T-UI-SHELL) enforces this.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";
import { signOut } from "../../utils/auth";
import PortalNav from "./PortalNav";
import { secondaryButton } from "./styles";

export default function PortalShell({
  supplierName,
  customerName,
  invoicesAvailable,
  children,
}: {
  supplierName: string;
  customerName: string;
  invoicesAvailable: boolean;
  children: React.ReactNode;
}) {
  const router = useRouter();
  const [signingOut, setSigningOut] = useState(false);

  const handleSignOut = async () => {
    if (signingOut) return;
    setSigningOut(true);
    try {
      await signOut();
    } finally {
      router.replace("/signin");
    }
  };

  return (
    <div className="min-h-screen bg-gray-50 text-gray-900">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-white focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-primary focus:shadow"
      >
        Skip to content
      </a>
      <header className="border-b border-gray-200 bg-white">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="min-w-0">
            <p className="text-sm text-gray-700">{supplierName}</p>
            <p className="break-words text-lg font-semibold text-gray-900">
              {customerName}
            </p>
          </div>
          <button
            type="button"
            onClick={handleSignOut}
            disabled={signingOut}
            className={secondaryButton}
          >
            Sign out
          </button>
        </div>
      </header>
      <PortalNav invoicesAvailable={invoicesAvailable} />
      <main
        id="main"
        tabIndex={-1}
        className="mx-auto max-w-5xl px-4 py-6 focus:outline-none"
      >
        {children}
      </main>
    </div>
  );
}
