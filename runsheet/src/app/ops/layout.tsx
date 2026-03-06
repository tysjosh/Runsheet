"use client";

import { lazy, Suspense, useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import ErrorBoundary from "../../components/ErrorBoundary";
import Header from "../../components/Header";
import Sidebar from "../../components/Sidebar";

const AIChat = lazy(() => import("../../components/AIChat"));

function getActiveOpsItem(pathname: string): string {
  if (pathname.startsWith("/ops/fuel")) return "ops-fuel";
  if (pathname.startsWith("/ops/scheduling")) return "ops-scheduling";
  if (pathname.startsWith("/ops/control")) return "ops-control";
  if (pathname.startsWith("/ops/riders")) return "ops-riders";
  if (pathname.startsWith("/ops/failures")) return "ops-failures";
  return "ops-shipments";
}

export default function OpsLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true);
  const [activeMenuItem, setActiveMenuItem] = useState(getActiveOpsItem(pathname));
  const [aiChatOpen, setAiChatOpen] = useState(false);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  // Auth check — same as main page
  useEffect(() => {
    if (typeof window !== "undefined") {
      const authStatus = localStorage.getItem("isAuthenticated");
      if (authStatus === "true") {
        setIsAuthenticated(true);
      } else {
        router.replace("/signin");
      }
      setIsLoading(false);
    }
  }, [router]);

  const handleMenuNavigation = (item: string) => {
    setActiveMenuItem(item);
    const nonOpsItems = ["upload-data", "inventory", "orders", "fleet", "analytics", "support"];
    if (nonOpsItems.includes(item)) {
      router.push("/");
    }
  };

  if (isLoading) {
    return (
      <div className="h-screen flex items-center justify-center bg-gray-50">
        <div className="text-center">
          <div className="w-8 h-8 border-4 border-gray-300 border-t-blue-600 rounded-full animate-spin mx-auto mb-4"></div>
          <p className="text-gray-600">Loading...</p>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) return null;

  return (
    <div className="h-screen flex flex-col bg-white">
      <div className="flex flex-1 overflow-hidden">
        <Sidebar
          activeItem={activeMenuItem}
          isCollapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
          onNavigate={handleMenuNavigation}
          opsEnabled={true}
        />
        <div className="flex-1 flex flex-col min-h-0 overflow-hidden" style={{ minWidth: 0 }}>
          <Header onAIClick={() => setAiChatOpen(true)} />
          <main className="flex-1 flex bg-white relative z-0 overflow-hidden">
            <div className="flex-1 flex bg-white overflow-auto">
              {children}
            </div>
          </main>
        </div>
      </div>
      <ErrorBoundary componentName="AI Chat">
        <Suspense fallback={null}>
          <AIChat isOpen={aiChatOpen} onClose={() => setAiChatOpen(false)} />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
