"use client";

/**
 * Top bar (R2.1): 48 px; global search with a `/` shortcut, the notification
 * bell, New order, Copilot and the user menu. No brand mark (it lives in the
 * sidebar) and no `<h1>`: each page owns the only `<h1>` in its title row.
 */
import { Menu as MenuIcon, Plus, Sparkles } from "lucide-react";
import GlobalSearch from "../GlobalSearch";
import NotificationBell from "../NotificationBell";
import UserMenu from "./UserMenu";

export interface TopBarProps {
  onAIClick: () => void;
  onMenuClick: () => void;
  onSearch: (query: string) => void;
  onNewOrder: () => void;
  onProfile: () => void;
  onSignOut: () => void;
  email?: string | null;
}

export default function TopBar({
  onAIClick,
  onMenuClick,
  onSearch,
  onNewOrder,
  onProfile,
  onSignOut,
  email,
}: TopBarProps) {
  return (
    <header
      data-chrome="topbar"
      className="flex h-12 shrink-0 items-center gap-2 border-b border-slate-200 bg-surface px-3 sm:px-4"
    >
      <button
        type="button"
        onClick={onMenuClick}
        aria-label="Open navigation menu"
        className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-700 hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus md:hidden"
      >
        <MenuIcon aria-hidden="true" className="h-5 w-5" />
      </button>

      <GlobalSearch onSubmitFallback={onSearch} />

      <div className="ml-auto flex items-center gap-1.5">
        <NotificationBell />
        <button
          type="button"
          onClick={onNewOrder}
          className="hidden h-8 items-center gap-1.5 rounded-lg bg-primary px-3 text-sm font-medium text-on-primary hover:bg-primary-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-1 sm:inline-flex"
        >
          <Plus aria-hidden="true" className="h-4 w-4" />
          New order
        </button>
        <button
          type="button"
          onClick={onAIClick}
          aria-label="Ask Copilot"
          className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-slate-300 px-2.5 text-sm font-medium text-slate-800 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        >
          <Sparkles aria-hidden="true" className="h-4 w-4 text-brand-600" />
          <span className="hidden lg:inline">Copilot</span>
        </button>
        <UserMenu email={email} onProfile={onProfile} onSignOut={onSignOut} />
      </div>
    </header>
  );
}
