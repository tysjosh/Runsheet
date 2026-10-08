"use client";

/**
 * Dashboard shell layout.
 *
 * Owns the persistent chrome shared by every `/dashboard/*` view: the
 * SuperTokens auth gate, the grouped sidebar, the 48 px top bar, the tenant
 * settings (time zone for `lib/format`), the app-wide toaster and the two
 * global overlays (Create Order modal + AI Copilot). Each module is its own
 * route segment rendered as `{children}`.
 */

import { usePathname, useRouter } from "next/navigation";
import { lazy, Suspense, useEffect, useState } from "react";
import Session from "supertokens-auth-react/recipe/session";
import ErrorBoundary from "../../components/ErrorBoundary";
import Sidebar from "../../components/shell/Sidebar";
import {
  TenantSettingsProvider,
  useTenantSettings,
} from "../../components/shell/TenantSettings";
import TopBar from "../../components/shell/TopBar";
import { useNavCounts } from "../../components/shell/useNavCounts";
import { InShellNavProvider } from "../../components/ui/InShellNav";
import { GlobalToaster } from "../../components/ui/toast/notify";
import { canSee, moduleDescriptor } from "../../config/modules";
import { NAV_SECTIONS, navIdForSegment } from "../../config/nav";
import { getCurrentUserRoles } from "../../utils/auth";
import {
  DashboardChromeProvider,
  dashboardActiveItem,
  dashboardHref,
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
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

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

  if (isLoading) {
    return (
      <div className="h-screen flex items-center justify-center bg-slate-50">
        <div className="text-center">
          <div className="w-8 h-8 border-4 border-slate-300 border-t-primary rounded-full animate-spin mx-auto mb-4"></div>
          <p className="text-slate-600">Loading...</p>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) return null;

  return (
    <TenantSettingsProvider>
      <Shell>{children}</Shell>
    </TenantSettingsProvider>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname() ?? "/dashboard";
  const { profile } = useTenantSettings();

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [aiChatOpen, setAiChatOpen] = useState(false);
  const [createOrderOpen, setCreateOrderOpen] = useState(false);
  // `null` until the session's claims resolve. Every visibility decision below
  // treats that as "no roles", so nothing role-gated renders early.
  const [roles, setRoles] = useState<readonly string[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const r = await getCurrentUserRoles();
      if (!cancelled) setRoles(r);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // The route's module segment (`control`, `orders`, …) gates access; the
  // sidebar highlights its nav alias (`control` is labelled Live).
  const activeItem = dashboardActiveItem(pathname);
  const activeNav =
    activeItem === "today" ? "today" : navIdForSegment(activeItem);

  const hasAnyNavAccess =
    roles === null ||
    NAV_SECTIONS.some((section) =>
      section.items.some((item) => canSee(item.id, { roles })),
    );

  const counts = useNavCounts(roles !== null && hasAnyNavAccess);

  // Route guard: a hidden module reached directly bounces to the dashboard
  // root. Only registered ids are guarded (profile has no registry entry).
  useEffect(() => {
    if (roles === null || !hasAnyNavAccess) return;
    if (!moduleDescriptor(activeItem)) return;
    if (canSee(activeItem, { roles })) return;
    router.replace("/dashboard");
  }, [roles, hasAnyNavAccess, activeItem, router]);

  const inShellNav = {
    handles: (type: string) => type === "order" || type === "customer",
    open: (type: string, id: string) => {
      if (type === "order")
        router.push(`/dashboard/orders/${encodeURIComponent(id)}`);
      else if (type === "customer")
        router.push(`/dashboard/customers/${encodeURIComponent(id)}`);
    },
    openModule: (item: string, tab?: string) => {
      router.push(dashboardHref(item, tab));
    },
  };

  const chrome = {
    openCreateOrder: () => setCreateOrderOpen(true),
    openAIChat: () => setAiChatOpen(true),
  };

  const signOut = async () => {
    try {
      await Session.signOut();
    } finally {
      router.push("/signin");
    }
  };

  // Nothing visible: a plain explanation rather than an empty shell. This is
  // what a driver-role account sees; drivers use the separate driver app.
  if (!hasAnyNavAccess) {
    return (
      <div className="h-screen flex items-center justify-center bg-slate-50 px-6">
        <div className="max-w-md text-center">
          <h1 className="text-xl font-semibold text-slate-900 mb-2">
            No modules available for your role
          </h1>
          <p className="text-slate-600 mb-6">
            This workspace is for dispatchers and administrators. If you drive
            for this company, use the Runsheet driver app instead. Otherwise ask
            an administrator to review your account&apos;s roles.
          </p>
          <button
            type="button"
            onClick={signOut}
            className="px-4 py-2 rounded-lg bg-primary text-white text-sm font-medium hover:bg-primary-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-focus"
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col bg-canvas">
      <div className="flex flex-1 overflow-hidden">
        <Sidebar
          activeItem={activeNav}
          roles={roles}
          counts={counts}
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
          <TopBar
            email={profile?.email}
            onAIClick={() => setAiChatOpen(true)}
            onMenuClick={() => setMobileSidebarOpen(true)}
            onSearch={(query = "") =>
              router.push(
                query
                  ? `/dashboard/orders?q=${encodeURIComponent(query)}`
                  : "/dashboard/orders",
              )
            }
            onNewOrder={() => setCreateOrderOpen(true)}
            onProfile={() => router.push("/dashboard/profile")}
            onSignOut={signOut}
          />
          <main
            id="main"
            className="flex-1 flex bg-white relative z-0 overflow-hidden"
          >
            <DashboardChromeProvider value={chrome}>
              <InShellNavProvider value={inShellNav}>
                {/* min-w-0 on the wrapper and its direct child: flex items
                    default to min-width:auto, so a wide table or header made
                    the page wider than the slot beside the sidebar and
                    clipped the right edge (R-4). Wide content scrolls inside
                    its own panel instead. */}
                <div className="flex-1 min-w-0 flex bg-white overflow-auto *:min-w-0">
                  {children}
                </div>
              </InShellNavProvider>
            </DashboardChromeProvider>
          </main>
        </div>
      </div>

      <GlobalToaster />
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
