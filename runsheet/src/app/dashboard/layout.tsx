"use client";

/**
 * Dashboard shell layout.
 *
 * Owns the persistent chrome shared by every `/dashboard/*` view — the
 * SuperTokens auth gate, the sidebar, the header, and the two global overlays
 * (Create Order modal + AI Copilot). Each module is now its own route segment
 * rendered as `{children}`, so navigation uses real URLs (deep-linkable,
 * back/forward, refresh-safe) instead of a single stateful switch.
 */

import { usePathname, useRouter } from "next/navigation";
import { lazy, Suspense, useEffect, useState } from "react";
import Session from "supertokens-auth-react/recipe/session";
import ErrorBoundary from "../../components/ErrorBoundary";
import Header from "../../components/Header";
import Sidebar, { NAV_SECTIONS } from "../../components/Sidebar";
import { InShellNavProvider } from "../../components/ui/InShellNav";
import { canSee, moduleDescriptor } from "../../config/modules";
import { getCurrentUserRoles } from "../../utils/auth";
import {
  DashboardChromeProvider,
  dashboardActiveItem,
  dashboardPathForItem,
} from "./shell-context";

const AIChat = lazy(() => import("../../components/AIChat"));
const CreateOrderModal = lazy(
  () => import("../../components/ops/CreateOrderModal"),
);

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname() ?? "/dashboard";

  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [aiChatOpen, setAiChatOpen] = useState(false);
  const [createOrderOpen, setCreateOrderOpen] = useState(false);
  // `null` until the session's claims resolve. Every visibility decision below
  // treats that as "no roles", so nothing role-gated renders early.
  const [roles, setRoles] = useState<readonly string[] | null>(null);

  useEffect(() => {
    // Verified SuperTokens session (cookie-managed by the SDK); bounce to
    // sign-in when absent.
    let cancelled = false;
    (async () => {
      try {
        const exists = await Session.doesSessionExist();
        if (cancelled) return;
        if (exists) setIsAuthenticated(true);
        else router.replace("/signin");
      } catch {
        if (!cancelled) router.replace("/signin");
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  useEffect(() => {
    if (!isAuthenticated) return;
    let cancelled = false;
    (async () => {
      const r = await getCurrentUserRoles();
      if (!cancelled) setRoles(r);
    })();
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated]);

  const activeItem = dashboardActiveItem(pathname);

  // Can this user reach anything at all? Answered only once roles resolve, so a
  // slow session does not flash a "no access" screen at a legitimate admin.
  const hasAnyNavAccess =
    roles === null ||
    NAV_SECTIONS.some((section) =>
      section.items.some((item) => canSee(item.id, { roles })),
    );

  // Route guard: a hidden module reached directly — typed URL, stale bookmark,
  // a link from before a role change — bounces to the dashboard root.
  //
  // Only *registered* ids are guarded. `/dashboard/orders` and
  // `/dashboard/profile` are not gateable modules and have no registry entry, so
  // `canSee` would fail them closed; skipping unregistered ids keeps the
  // fail-closed rule from deleting legitimate routes it was never asked about.
  useEffect(() => {
    if (roles === null || !hasAnyNavAccess) return;
    if (!moduleDescriptor(activeItem)) return;
    if (canSee(activeItem, { roles })) return;
    router.replace("/dashboard");
  }, [roles, hasAnyNavAccess, activeItem, router]);

  // In-shell navigation for EntityLink + the Storm_Mode banner: route to the
  // real `/dashboard/*` URL so the address bar reflects the view.
  const inShellNav = {
    handles: (type: string) => type === "order" || type === "customer",
    open: (type: string, id: string) => {
      if (type === "order")
        router.push(`/dashboard/orders/${encodeURIComponent(id)}`);
      else if (type === "customer")
        router.push(`/dashboard/customers/${encodeURIComponent(id)}`);
    },
    openModule: (item: string, tab?: string) => {
      const base = dashboardPathForItem(item);
      router.push(tab ? `${base}?tab=${encodeURIComponent(tab)}` : base);
    },
  };

  const chrome = {
    openCreateOrder: () => setCreateOrderOpen(true),
    openAIChat: () => setAiChatOpen(true),
  };

  if (isLoading) {
    return (
      <div className="h-screen flex items-center justify-center bg-gray-50">
        <div className="text-center">
          <div className="w-8 h-8 border-4 border-gray-300 border-t-primary rounded-full animate-spin mx-auto mb-4"></div>
          <p className="text-gray-600">Loading...</p>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) return null;

  // Nothing visible: render a plain explanation rather than an empty shell with
  // a blank sidebar. This is what a driver-role account sees — the web app is
  // the dispatcher/admin surface and drivers use the separate driver app.
  if (!hasAnyNavAccess) {
    return (
      <div className="h-screen flex items-center justify-center bg-gray-50 px-6">
        <div className="max-w-md text-center">
          <h1 className="text-xl font-semibold text-gray-900 mb-2">
            No modules available for your role
          </h1>
          <p className="text-gray-600 mb-6">
            This workspace is for dispatchers and administrators. If you drive
            for this company, use the Runsheet driver app instead. Otherwise ask
            an administrator to review your account&apos;s roles.
          </p>
          <button
            type="button"
            onClick={async () => {
              await Session.signOut();
              router.push("/signin");
            }}
            className="px-4 py-2 rounded-lg bg-primary text-white text-sm font-medium hover:bg-primary-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-[color:var(--color-primary)]"
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col bg-white">
      <div className="flex flex-1 overflow-hidden">
        <Sidebar
          activeItem={activeItem}
          isCollapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
          onNavigate={(item) => router.push(dashboardPathForItem(item))}
          isMobileOpen={mobileSidebarOpen}
          onMobileClose={() => setMobileSidebarOpen(false)}
        />
        <div
          className="flex-1 flex flex-col min-h-0 overflow-hidden"
          style={{ minWidth: 0 }}
        >
          <Header
            onAIClick={() => setAiChatOpen(true)}
            onMenuClick={() => setMobileSidebarOpen(true)}
            onSearch={(query = "") =>
              router.push(
                query
                  ? `/dashboard/orders?q=${encodeURIComponent(query)}`
                  : "/dashboard/orders",
              )
            }
            onNewOrder={() => {
              setCreateOrderOpen(true);
              router.push("/dashboard/orders");
            }}
          />
          <main className="flex-1 flex bg-white relative z-0 overflow-hidden">
            <DashboardChromeProvider value={chrome}>
              <InShellNavProvider value={inShellNav}>
                <div className="flex-1 flex bg-white overflow-auto">
                  {children}
                </div>
              </InShellNavProvider>
            </DashboardChromeProvider>
          </main>
        </div>
      </div>

      <ErrorBoundary componentName="AI Chat">
        <Suspense fallback={null}>
          <AIChat isOpen={aiChatOpen} onClose={() => setAiChatOpen(false)} />
        </Suspense>
      </ErrorBoundary>
      <ErrorBoundary componentName="Create Order">
        <Suspense fallback={null}>
          <CreateOrderModal
            isOpen={createOrderOpen}
            onClose={() => setCreateOrderOpen(false)}
            onSuccess={(orderId) => {
              setCreateOrderOpen(false);
              router.push(`/dashboard/orders/${encodeURIComponent(orderId)}`);
            }}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
