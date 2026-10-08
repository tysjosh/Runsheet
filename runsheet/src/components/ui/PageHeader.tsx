"use client";

/**
 * PageHeader: the compact 44 px title row (R4.1, R4.2, design.md §3).
 *
 * One `<h1>` (16 px), then inline view tabs, a context slot (date stepper and
 * the like), inline counts, and up to two primary actions on the right. No
 * subtitle and no hero icon: `subtitle` is still accepted so untouched pages
 * compile, and feeds the ⓘ help tooltip when `help` is absent; `icon` is
 * ignored.
 *
 * Embedded mode. A hub wraps its header and content in `PageChromeProvider`
 * and marks its own header `host`. Any `PageHeader` rendered inside that
 * content (a tab page that still has its own header) renders nothing and
 * contributes its actions, counts and context to the host row instead, so a
 * page never stacks two headers or two `<h1>`s. Inner pages can also call
 * `usePageChrome()` directly.
 */
import { ArrowLeft, Info } from "lucide-react";
import Link from "next/link";
import {
  createContext,
  type ReactNode,
  useContext,
  useId,
  useLayoutEffect,
  useState,
  useSyncExternalStore,
} from "react";
import { type TabItem, Tabs } from "./Tabs";
import { Tooltip } from "./Tooltip";

export interface PageHeaderProps {
  title: string;
  /** ⓘ tooltip next to the title. */
  help?: ReactNode;
  /** Not rendered; becomes `help` when `help` is absent. */
  subtitle?: string;
  /** Not rendered (kept so existing call sites compile). */
  icon?: ReactNode;
  /** View tabs rendered inline in the title row. */
  tabs?: TabItem[];
  /** Controlled tab value; without it the tabs bind to `?tab=`. */
  tab?: string;
  onTabChange?: (id: string) => void;
  tabAliases?: Record<string, string>;
  /** Id base linking the tabs to a `TabPanel`. */
  tabIdBase?: string;
  context?: ReactNode;
  counts?: ReactNode;
  /** At most two primary actions. */
  actions?: ReactNode;
  back?: { href: string; label: string };
  badge?: ReactNode;
  className?: string;
  /** This header hosts contributions from embedded content (see above). */
  host?: boolean;
}

// ── page chrome store ────────────────────────────────────────────────────

export interface ChromeContribution {
  actions?: ReactNode;
  counts?: ReactNode;
  context?: ReactNode;
}

interface ChromeStore {
  set: (id: string, c: ChromeContribution) => void;
  remove: (id: string) => void;
  subscribe: (fn: () => void) => () => void;
  snapshot: () => Map<string, ChromeContribution>;
}

function createChromeStore(): ChromeStore {
  let map = new Map<string, ChromeContribution>();
  const listeners = new Set<() => void>();
  const emit = () => {
    for (const l of listeners) l();
  };
  return {
    set(id, c) {
      map = new Map(map);
      map.set(id, c);
      emit();
    },
    remove(id) {
      if (!map.has(id)) return;
      map = new Map(map);
      map.delete(id);
      emit();
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    snapshot: () => map,
  };
}

const ChromeContext = createContext<ChromeStore | null>(null);
const EMPTY = new Map<string, ChromeContribution>();
const noopSubscribe = () => () => {};

/** Wrap a hub's header and its tab content. */
export function PageChromeProvider({ children }: { children: ReactNode }) {
  const [store] = useState(createChromeStore);
  return (
    <ChromeContext.Provider value={store}>{children}</ChromeContext.Provider>
  );
}

/**
 * Contribute actions/counts/context to the hub's title row. Returns true when
 * a host exists (so the caller can skip rendering its own header).
 */
export function usePageChrome(contribution: ChromeContribution): boolean {
  const store = useContext(ChromeContext);
  const id = useId();
  // Runs every render so the host always shows the latest nodes. Only the
  // host subscribes, so this cannot loop.
  useLayoutEffect(() => {
    store?.set(id, contribution);
  });
  useLayoutEffect(() => () => store?.remove(id), [store, id]);
  return store !== null;
}

function useContributions(enabled: boolean) {
  const store = useContext(ChromeContext);
  return useSyncExternalStore(
    enabled && store ? store.subscribe : noopSubscribe,
    enabled && store ? store.snapshot : () => EMPTY,
    () => EMPTY,
  );
}

// ── header ───────────────────────────────────────────────────────────────

export function PageHeader(props: PageHeaderProps) {
  const store = useContext(ChromeContext);
  if (store && !props.host) return <EmbeddedHeader {...props} />;
  return <HeaderRow {...props} />;
}

function EmbeddedHeader({ actions, counts, badge, context }: PageHeaderProps) {
  usePageChrome({
    actions,
    counts:
      badge || counts ? (
        <>
          {badge}
          {counts}
        </>
      ) : undefined,
    context,
  });
  return null;
}

function HeaderRow({
  title,
  help,
  subtitle,
  tabs,
  tab,
  onTabChange,
  tabAliases,
  tabIdBase,
  context,
  counts,
  actions,
  back,
  badge,
  className = "",
  host = false,
}: PageHeaderProps) {
  const contributions = useContributions(host);
  const extra = [...contributions.values()];
  const helpText = help ?? subtitle;
  return (
    <div
      data-chrome="titlerow"
      className={`flex h-11 shrink-0 items-center gap-3 border-b border-slate-200 bg-surface px-4 ${className}`}
    >
      {back && (
        <Link
          href={back.href}
          className="inline-flex h-7 shrink-0 items-center gap-1 rounded-lg px-1.5 text-xs font-medium text-link hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        >
          <ArrowLeft aria-hidden="true" className="h-3.5 w-3.5" />
          {back.label}
        </Link>
      )}
      <div className="flex min-w-0 shrink-0 items-center gap-1.5">
        <h1 className="truncate text-base font-semibold text-text">{title}</h1>
        {helpText && (
          <Tooltip content={helpText}>
            <button
              type="button"
              aria-label={`About ${title}`}
              className="inline-flex h-5 w-5 items-center justify-center rounded-full text-slate-500 hover:text-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              <Info aria-hidden="true" className="h-3.5 w-3.5" />
            </button>
          </Tooltip>
        )}
        {badge}
      </div>
      {tabs && tabs.length > 0 && (
        <Tabs
          tabs={tabs}
          value={tab}
          onChange={onTabChange}
          aliases={tabAliases}
          idBase={tabIdBase}
          label={`${title} views`}
        />
      )}
      {context && (
        <div className="flex min-w-0 items-center gap-2">{context}</div>
      )}
      {extra.map((c, i) =>
        c.context ? (
          <div key={`ctx-${i}`} className="flex min-w-0 items-center gap-2">
            {c.context}
          </div>
        ) : null,
      )}
      {(counts || extra.some((c) => c.counts)) && (
        <div className="flex min-w-0 items-center gap-2 overflow-hidden text-xs text-text-muted">
          {counts}
          {extra.map((c, i) =>
            c.counts ? <span key={`cnt-${i}`}>{c.counts}</span> : null,
          )}
        </div>
      )}
      <div className="ml-auto flex shrink-0 items-center gap-2">
        {extra.map((c, i) =>
          c.actions ? (
            <div key={`act-${i}`} className="flex items-center gap-2">
              {c.actions}
            </div>
          ) : null,
        )}
        {actions}
      </div>
    </div>
  );
}
