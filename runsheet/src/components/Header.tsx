"use client";

import { Menu, Plus, Sparkles } from "lucide-react";
import { useRouter } from "next/navigation";
import GlobalSearch from "./GlobalSearch";

interface HeaderProps {
  onAIClick?: () => void;
  /** Open the off-canvas sidebar drawer. Only wired below `md`. */
  onMenuClick?: () => void;
  /** In-shell search handler. Falls back to routing to the Orders board. */
  onSearch?: (query: string) => void;
  /** In-shell "New Order" handler. Falls back to routing to the Orders board. */
  onNewOrder?: () => void;
}

export default function Header({
  onAIClick,
  onMenuClick,
  onSearch,
  onNewOrder,
}: HeaderProps) {
  const router = useRouter();

  // Enter with no obvious single match hands the query to the orders board
  // (the in-shell handler when provided, else the standalone route).
  const handleSearchAll = (q: string) => {
    if (onSearch) {
      onSearch(q);
      return;
    }
    router.push(q ? `/ops?q=${encodeURIComponent(q)}` : "/ops");
  };

  const handleNewOrder = () => {
    if (onNewOrder) onNewOrder();
    else router.push("/ops");
  };

  return (
    <header
      className="flex-shrink-0 border-b bg-[color:var(--color-surface)]"
      style={{
        borderColor:
          "color-mix(in srgb, var(--color-primary) 10%, transparent)",
      }}
    >
      <div className="flex h-16 items-center gap-3 px-4 sm:px-6 lg:px-8">
        {/* Mobile drawer toggle — hidden on the desktop rail (md+) */}
        <button
          type="button"
          onClick={onMenuClick}
          aria-label="Open navigation menu"
          className="md:hidden flex items-center justify-center w-9 h-9 rounded-lg transition-colors hover:bg-[color-mix(in_srgb,var(--color-primary)_8%,transparent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-[color:var(--color-primary)]"
          style={{ color: "var(--color-primary)" }}
        >
          <Menu className="w-5 h-5" />
        </button>

        {/* Brand */}
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-xl flex items-center justify-center bg-primary">
            <img
              src="/runsheet_logo.svg"
              alt="Runsheet"
              className="w-5 h-5"
              style={{ filter: "brightness(0) invert(1)" }}
            />
          </div>
          <h1
            className="hidden text-lg font-semibold tracking-tight sm:block"
            style={{ color: "var(--color-primary)" }}
          >
            Runsheet
          </h1>
        </div>

        {/* Global search — flexes to fill the bar */}
        <GlobalSearch onSubmitFallback={handleSearchAll} />

        {/* Actions */}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={handleNewOrder}
            className="hidden items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium text-white transition-colors bg-primary hover:bg-primary-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-[color:var(--color-primary)] sm:flex"
          >
            <Plus className="h-4 w-4" />
            New Order
          </button>
          <button
            type="button"
            onClick={onAIClick}
            aria-label="Ask the AI copilot"
            className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition-colors hover:bg-[color-mix(in_srgb,var(--color-primary)_6%,transparent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-[color:var(--color-primary)]"
            style={{
              color: "var(--color-primary)",
              borderColor:
                "color-mix(in srgb, var(--color-primary) 12%, transparent)",
            }}
          >
            <Sparkles
              className="h-4 w-4"
              style={{ color: "var(--color-brand-accent)" }}
            />
            <span className="hidden sm:inline">Ask Copilot</span>
          </button>
        </div>
      </div>
    </header>
  );
}
